from math import sqrt

import numpy as np
import torch
import torch.autograd as autograd
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable
from torch.distributions import Categorical
from torch.utils.data import DataLoader, Dataset
import math
from modules import Glimpse, GraphEmbedding, Pointer

NUM_SAMPLING = 10

class Normalization(nn.Module):
    # https://github.com/wouterkool/attention-learn-to-route/blob/ffd5b862f5b12867d82cfa9ea78344cc0d1bb4b8/nets/graph_encoder.py#L114
    def __init__(self, embed_dim, normalization='batch'):
        super(Normalization, self).__init__()
        normalizer_class = {
            'batch': nn.BatchNorm1d,
            'instance': nn.InstanceNorm1d
        }.get(normalization, None)

        self.normalizer = normalizer_class(embed_dim, affine=True)
        for name, param in self.named_parameters():
            stdv = 1. / math.sqrt(param.size(-1))
            param.data.uniform_(-stdv, stdv)
        # Normalization by default initializes affine parameters with bias 0 and weight unif(0,1) which is too large!
        # self.init_parameters()

    def forward(self, input):
        if isinstance(self.normalizer, nn.BatchNorm1d):
            return self.normalizer(input.view(-1, input.size(-1))).view(*input.size())
        elif isinstance(self.normalizer, nn.InstanceNorm1d):
            return self.normalizer(input.permute(0, 2, 1)).permute(0, 2, 1)
        else:
            assert self.normalizer is None, "Unknown normalizer type"
            return input


class att_layer(nn.Module):
    def __init__(self, embed_dim, n_heads, feed_forward_hidden=512, normalizer='batch'):
        super(att_layer, self).__init__()
        self.mha = torch.nn.MultiheadAttention(embed_dim, n_heads)
        self.embed = nn.Sequential(nn.Linear(embed_dim, feed_forward_hidden), nn.ReLU(), nn.Linear(feed_forward_hidden, embed_dim))
        self.normalizer = Normalization(feed_forward_hidden, normalizer)

    def forward(self, x):
        # Multiheadattention in pytorch starts with (target_seq_length, batch_size, embedding_size).
        # thus we permute order first. https://pytorch.org/docs/stable/nn.html#multiheadattention
        x = x.permute(1, 0, 2)
        _1 = x + self.mha(x, x, x)[0]
        _1 = _1.permute(1, 0, 2)
        _2 = _1 + self.embed(_1)
        return self.normalizer(_2)


class AttentionModule(nn.Sequential):
    def __init__(self, embed_dim, n_heads, feed_forward_hidden=512, n_self_attentions=2, bn='batch'):
        super(AttentionModule, self).__init__(
            *(att_layer(embed_dim, n_heads, feed_forward_hidden, bn) for _ in range(n_self_attentions))
        )


class AttentionTSP(nn.Module):
    def __init__(self,
                 input_dim,
                 embedding_size,
                 hidden_size,
                 seq_len,
                 n_head=4,
                 C=10,
                 use_cuda=True, ret_embedded_vector=False,
                 only_encoder=False):
        super(AttentionTSP, self).__init__()
        self.ret_embedded_vector = ret_embedded_vector
        self.embedding_size = embedding_size
        self.hidden_size = hidden_size
        self.seq_len = seq_len
        self.n_head = n_head
        self.C = C
        self.use_cuda = use_cuda
        self.only_encoder = only_encoder
        if self.only_encoder:
            self.linear_layer = nn.Linear(self.embedding_size, 1)
        self.embedding = GraphEmbedding(input_dim, embedding_size)
        self.mha = AttentionModule(embedding_size, n_head, hidden_size)

        self.init_w = nn.Parameter(torch.Tensor(self.embedding_size))
        self.init_w.data.uniform_(-0.1, 0.1)
        self.glimpse = Glimpse(self.embedding_size, self.hidden_size, self.n_head)
        self.pointer = Pointer(self.embedding_size, self.hidden_size, 1, self.C)

        self.h_context_embed = nn.Linear(self.embedding_size, self.embedding_size)
        self.v_weight_embed = nn.Linear(self.embedding_size, self.embedding_size)
        self.h_query_embed = nn.Linear(self.embedding_size, self.embedding_size)
        self.memory_transform = nn.Linear(self.embedding_size, self.embedding_size)
        self.chosen_transform = nn.Linear(self.embedding_size, self.embedding_size)
        self.h1_transform = nn.Linear(self.embedding_size, self.embedding_size)
        self.h2_transform = nn.Linear(self.embedding_size, self.embedding_size)

    def forward(self, inputs, argmax=False, guide=None, multisampling=False):
        """
        Args:
            inputs: [batch_size x seq_len x 2]
            guide:
        """
        if multisampling:
            return self.multisampling(inputs)
        # self.ret_embedded_vector=True
        batch_size = inputs.shape[0]
        seq_len = inputs.shape[1]
        embedded, h, h_mean, h_bar, chosen_vector, left_vector, query = self._prepare(inputs)
        #init query
        # if self.ret_embedded_vector:
        #     return embedded

        prev_chosen_indices = []
        prev_chosen_logprobs = []
        mask = torch.zeros(batch_size, seq_len, dtype=torch.bool)

        # if self.only_encoder:
        #     prob = torch.softmax(self.linear_layer(h).squeeze(), -1)       # [batch_size x seq_len]
        #     for index in range(seq_len):
        #         cat = Categorical(prob)
        #         if argmax:
        #             _, chosen = torch.max(prob, -1)
        #         elif guide is not None:
        #             chosen = guide[:, index]
        #         else:
        #             chosen = cat.sample()
        #         logprobs = cat.log_prob(chosen)
        #         logprobs = cat.log_prob(chosen)
        #         prev_chosen_indices.append(chosen)
        #         prev_chosen_logprobs.append(logprobs)
        #         prob[chosen] = 0
        #         print(prob)
        #         print(prob.size())
        #         print(torch.sum(prob, -1).size())
        #         prob = prob / torch.sum(prob, -1).unsqueeze(1)

        cumulated_distributions = []
        for index in range(seq_len):
            i = index
            _, n_query = self.glimpse(query, h, mask)
            prob, _ = self.pointer(n_query, h, mask)        # [batch size x num_tasks]
            # cumulated_distributions.append(self.pointer(n_query, h, mask, ret_score=True))
            cumulated_distributions.append(prob)
            cat = Categorical(prob)
            if argmax:
                _, chosen = torch.max(prob, -1)
            elif guide is not None:
                chosen = guide[:, index]
            else:
                if not multisampling:
                    chosen = cat.sample()               # [batch_size].
                if multisampling:
                    pass
            logprobs = cat.log_prob(chosen)
            prev_chosen_indices.append(chosen)
            prev_chosen_logprobs.append(logprobs)


            mask[[i for i in range(batch_size)], chosen] = True

            cc = chosen.unsqueeze(1).unsqueeze(2).repeat(1, 1, self.embedding_size)
            chosen_hs = h.gather(1, cc).squeeze(1)
            chosen_vector = chosen_vector + self.chosen_transform(chosen_hs)
            left_vector = left_vector - self.memory_transform(chosen_hs)
            h1 = self.h1_transform(torch.tanh(chosen_vector))
            h2 = self.h2_transform(torch.tanh(left_vector))
            v_weight = self.v_weight_embed(chosen_hs)
            query = self.h_query_embed(h1 + h2 + v_weight)
        if self.ret_embedded_vector:        # KL Diverge용
            return torch.stack(prev_chosen_logprobs, 1), torch.stack(prev_chosen_indices, 1), torch.stack(cumulated_distributions, dim=1)
        return torch.stack(prev_chosen_logprobs, 1), torch.stack(prev_chosen_indices, 1)

    def beam_search(self, inputs, beam_size=3, num_candidates=20):
        batch_size = inputs.shape[0]
        seq_len = inputs.shape[1]
        #embedded, h, h_mean, h_bar, h_rest, query = self._prepare(inputs)
        #beam_candidates = np.zeros(shape=(batch_size, seq_len, beam_size))
        return self._prepare(inputs)


    def _prepare(self, inputs):
        batch_size = inputs.shape[0]
        seq_len = inputs.shape[1]

        embedded = self.embedding(inputs)
        h = self.mha(embedded)
        h_mean = h.mean(dim=1)
        h_bar = self.h_context_embed(h_mean)
        v_weight = self.v_weight_embed(self.init_w)
        chosen_vector = torch.zeros((batch_size, self.embedding_size))
        if self.use_cuda:
            chosen_vector = chosen_vector.cuda()
        left_vector = self.memory_transform(h).sum(dim=1)
        h1 = self.h1_transform(torch.tanh(chosen_vector)) #H_o
        h2 = self.h2_transform(torch.tanh(left_vector)) #H_l
        query = self.h_query_embed(h1 + h2 + v_weight) #c 
        return embedded, h, h_mean, h_bar, chosen_vector, left_vector, query

    def multisampling(self, inputs):
        batch_size = inputs.shape[0]
        seq_len = inputs.shape[1]

        embedded, h, h_mean, h_bar, chosen_vector, left_vector, query = self._prepare(inputs)
        # init query
        # print(h.size())       # [batch x num_task x embedding size]
        # print(h_mean.size())      # [batch x embedding size]
        # print(chosen_vector.size())   # [batch x emb size]
        # print(left_vector.size())     # [batch x emb size]
        # print(query.size())           # [batch x emb size]   ?
        h_ext = h.unsqueeze(1).repeat(1, NUM_SAMPLING, 1, 1)        # [batch x NUM_SAMPLING x num_task x emb_size]
        h_ext = h_ext.view(batch_size * NUM_SAMPLING, seq_len, -1)  # [batch x NUM_SAMPLING, num_task, emb_size]

        query_ext = query.unsqueeze(1).repeat(1, NUM_SAMPLING, 1)   # [batch, NUM_SAMPLING, emb_size]
        query_ext = query_ext.view(batch_size * NUM_SAMPLING, -1)   # [batch x NUM_SAMPLING, emb size]

        h_mean = h_mean.unsqueeze(1).repeat(1, NUM_SAMPLING, 1)
        h_mean.view(batch_size * NUM_SAMPLING, -1)

        chosen_vector = chosen_vector.unsqueeze(1).repeat(1, NUM_SAMPLING, 1)
        chosen_vector = chosen_vector.view(batch_size * NUM_SAMPLING, -1)

        left_vector = left_vector.unsqueeze(1).repeat(1, NUM_SAMPLING, 1)
        left_vector = left_vector.view(batch_size * NUM_SAMPLING, -1)

        prev_chosen_indices = []
        prev_chosen_logprobs = []
        mask = torch.zeros(batch_size * NUM_SAMPLING, seq_len, dtype=torch.bool)
        for index in range(seq_len):
            i = index
            _, n_query = self.glimpse(query_ext, h_ext, mask)       # [batch x emb size]
            prob, _ = self.pointer(n_query, h_ext, mask)  # [batch_size x NUM_SAMPLING, seq_len]
            cat = Categorical(prob)
            chosen = cat.sample()               # [batch_size x NUM_SAMPLING]
            chosen = chosen.squeeze()
            logprobs = cat.log_prob(chosen)
            prev_chosen_indices.append(chosen)
            prev_chosen_logprobs.append(logprobs)

            mask[[i for i in range(batch_size * NUM_SAMPLING)], chosen] = True

            cc = chosen.unsqueeze(1).unsqueeze(2).repeat(1, 1, self.embedding_size)
            chosen_hs = h_ext.gather(1, cc).squeeze(1)
            chosen_vector = chosen_vector + self.chosen_transform(chosen_hs)
            left_vector = left_vector - self.memory_transform(chosen_hs)
            h1 = self.h1_transform(torch.tanh(chosen_vector))
            h2 = self.h2_transform(torch.tanh(left_vector))
            v_weight = self.v_weight_embed(chosen_hs)
            query = self.h_query_embed(h1 + h2 + v_weight)

        return torch.stack(prev_chosen_indices, 1).view(batch_size, NUM_SAMPLING, seq_len)