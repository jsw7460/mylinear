"""
LINEAR TRAIN에서 복사ㅏㅏㅏㅏㅏㅏㅏㅏㅏㅏ
"""
import time
import argparse
import pickle
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
from tqdm import tqdm

import cy_heuristics as heu
from fast_soft_sort.pytorch_ops import soft_rank
from linearsolver import LinearSolver
from sched_solver import Solver
from util import get_util_range, Datasets

test_module = heu.test_RTA_LC
parser = argparse.ArgumentParser()

parser.add_argument("--num_tasks", type=int, default=32)
parser.add_argument("--num_procs", type=int, default=4)
parser.add_argument("--num_epochs", type=int, default=1500)
parser.add_argument("--num_train_dataset", type=int, default=200000)
parser.add_argument("--num_test_dataset", type=int, default=50)
parser.add_argument("--embedding_size", type=int, default=128)
parser.add_argument("--hidden_size", type=int, default=128)
parser.add_argument("--batch_size", type=int, default=20)
parser.add_argument("--grad_clip", type=float, default=1.5)
parser.add_argument("--lr", type=float, default=1e-4)
parser.add_argument("--lr_decay_step", type=int, default=100)
parser.add_argument("--use_deadline", action="store_true")
parser.add_argument("--range_l", type=str, default="2.70")
parser.add_argument("--range_r", type=str, default="2.70")
parser.add_argument("--use_cuda", action="store_true")
parser.add_argument("--load", type=int, default=-1)
parser.add_argument("--positive", action="store_true")

confidence = 0.05

args = parser.parse_args()
use_deadline = args.use_deadline
use_cuda = True
positive = False
DEBUG = False
if DEBUG:
    positive = True

fname = "LIN-p%d-t%d-d%d-l[%s, %s]" % (
            args.num_procs, args.num_tasks, int(use_deadline), args.range_l, args.range_r)


if __name__ == "__main__":
    if positive:
        print("Train with positive sampling")
    else:
        print("Train with all data")

    util_range = get_util_range(args.num_procs)
    trsets = []
    tesets = []
    on = False
    for util in util_range:
        on = False
        if util == args.range_l:
            on = True
        if on:
            if positive:
                load_file_name = "../Pandadata/tr/%d-%d/positive/%s"
            else:
                load_file_name = "../Pandadata/tr/%d-%d/%s"
            with open(load_file_name % (args.num_procs, args.num_tasks, util), 'rb') as f:
                ts = pickle.load(f)
                trsets.append(ts)
            with open("../Pandadata/te/%d-%d/%s" % (args.num_procs, args.num_tasks, util), 'rb') as f:
                ts = pickle.load(f)
                tesets.append(ts)
        if util == args.range_r:
            break


    train_dataset = Datasets(trsets)
    test_dataset = Datasets(tesets)
    train_dataset.setlen(args.num_train_dataset)
    test_dataset.setlen(args.num_test_dataset)

    train_loader = DataLoader(
        train_dataset,
        batch_size=args.batch_size,
        shuffle=False,
        pin_memory=True
    )
    test_loader = DataLoader(
        test_dataset,
        batch_size=args.batch_size,
        shuffle=False,
        pin_memory=True
    )
    eval_loader = DataLoader(
        test_dataset,
        batch_size=args.batch_size,
        shuffle=False,
        pin_memory=True
    )


    def wrap(x):
        _sample, num_proc, use_deadline = x
        return heu.OPA(_sample, num_proc, None, use_deadline)

    with ProcessPoolExecutor(max_workers=10) as executor:
        inputs = []
        res_opa = np.zeros(len(test_dataset), dtype=int).tolist()
        for i, sample in test_dataset:
            inputs.append((sample, args.num_procs, use_deadline))
        for i, ret in tqdm(enumerate(executor.map(wrap, inputs))):
            res_opa[i] = ret
        opares = np.sum(res_opa)
    print("[before training][OPA generates %d]" % opares)

    temp_fname = "localRL-p%d-t%d-d%d-l[%s, %s].torchmodel" % \
                 (args.num_procs, args.num_tasks, int(use_deadline), args.range_l, args.range_r)
    model = torch.load("../Pandamodels/localrlmodels/" + temp_fname).cuda()

    rl_model = Solver(
        args.num_procs,
        args.embedding_size,
        args.hidden_size,
        args.num_tasks,
        use_deadline=False,
        use_cuda=True
    )
    rl_model.load_state_dict(model.state_dict())
    if use_cuda:
        model = model.cuda()
        rl_model = rl_model.cuda()

    rl_model = rl_model.eval()

    ret = []
   # for i, _batch in eval_loader:
   #     if use_cuda:
   #         _batch = _batch.cuda()
   #     R, log_prob, actions = model(_batch, argmax=True)
   #     for j, chosen in enumerate(actions.cpu().numpy()):
   #         order = np.zeros_like(chosen)
   #         for p in range(args.num_tasks):
   #             order[chosen[p]] = args.num_tasks - p - 1
   #         if use_cuda:
   #             ret.append(test_module(_batch[j].cpu().numpy(), args.num_procs, order, use_deadline, False))
   #         else:
   #             ret.append(test_module(_batch[j].numpy(), args.num_procs, order, use_deadline, False))

   # print("[Before training][RL model generates %d]" % (np.sum(ret)))

    linear_model = LinearSolver(args.num_procs, args.num_tasks,
                                args.use_deadline, use_cuda)

    # TRAIN LOOP
    if use_cuda:
        linear_model = linear_model.to("cuda:0")
        rl_model = rl_model.to("cuda:0")

    linear_model = linear_model.train()
    criterion = nn.MSELoss()
    optimizer = optim.Adam(linear_model.parameters(), lr=5e-3)

    start = time.time()
    for epoch in range(args.num_epochs):
        loss_ = 0
        avg_hit = []
        for batch_idx, (_, sample_batch) in enumerate(train_loader):
            optimizer.zero_grad()
            rewards, probs, action = rl_model(sample_batch)
            # rl_order = rl_label[batch_idx]
            rl_order = torch.zeros_like(action)
            for i in range(rl_order.size(0)):  # batch size
                for j in range(rl_order.size(1)):  # num_tasks
                    rl_order[i][action[i][j]] = args.num_tasks - j - 1
            rl_order = soft_rank(rl_order.cpu(), regularization_strength=0.001).float()
            linear_score = linear_model(sample_batch)
            lin_soft_score = soft_rank(linear_score.cpu(), regularization_strength=0.001).float()
            # 점수가 높으면 더 중요하다. 즉 우선순위가 더 높다
            if use_cuda:
                lin_soft_score = lin_soft_score.to("cuda:0")
                rl_order = rl_order.to("cuda:0")
            loss = criterion(rl_order, lin_soft_score)
            loss.backward()
            print(epoch, loss)
            loss_ += loss / args.batch_size
            optimizer.step()
            # scheduler.step()
            if batch_idx % 10 == 0:
                with open("cumloss/soft/" + fname, "a") as f:
                    print("EPOCH:{}, LOSS:{:.3f}".format(epoch, loss_ / (batch_idx+1)), file=f)
        endtime = time.time()
        elapsed = (endtime - start)
        minute = int(elapsed // 60)
        second = int(elapsed - 60 * minute)
        # EVALUATE
        # linear_model.eval()
        # lin_ret = []
        # for i, _batch in eval_loader:
        #     if use_cuda:
        #         _batch = _batch.to("cuda:0")
        #     ev_linear_score = linear_model(_batch)
        #     _, ev_linear_score_idx = torch.sort(ev_linear_score, descending=True)
        #     np_linear_score = ev_linear_score_idx.cpu().detach().numpy()
        #     for j, chosen in enumerate(np_linear_score):
        #         order = np.zeros_like(chosen)
        #         for p in range(args.num_tasks):
        #             order[chosen[p]] = args.num_tasks - p - 1
        #         if use_cuda:
        #             lin_ret.append(
        #                 test_module(_batch[j].cpu().numpy(), args.num_procs,
        #                             order, use_deadline=False, ret_score=False))
        #         else:
        #             lin_ret.append(
        #                 test_module(
        #                     _batch[j].numpy(), args.num_procs, order, False, False))
        # if not positive:
        #     print("EPOCH : {} / RL MODEL GENERATES : {} / LINEAR MODEL GENERATES : {} / OPA GENERATES : {}. Not positive sampling".format(
        #         epoch, np.sum(ret), np.sum(lin_ret), opares
        #     ))
        # else:
        #     print(
        #         "EPOCH : {} / RL MODEL GENERATES : {} / LINEAR MODEL GENERATES : {} / OPA GENERATES : {}. With positive sampling".format(
        #             epoch, np.sum(ret), np.sum(lin_ret), opares
        #         ))
        # print("경과시간 : {}m{:}s".format(minute, second))
        # if epoch % 1 == 0:
        #     fname = "LIN-p%d-t%d-d%d-l[%s, %s]" \
        #             % (args.num_procs, args.num_tasks, int(use_deadline), args.range_l, args.range_r)
        #     torch.save(linear_model, "../Pandamodels/linearmodels/" + fname + ".torchmodel")
        #     print("SAVE SUCCESS")
        #     with open("log/softlog/" + fname, "a") as f:
        #         print("EPOCH : {} / RL MODEL GENERATES : {} / LINEAR MODEL GENERATES : {} / OPA GENERATES : {}".format(
        #             epoch, np.sum(ret), np.sum(lin_ret), opares
        #         ), file=f)
        #         print("경과시간 : {}m{:}s".format(minute, second), file=f)

        linear_model.train()
