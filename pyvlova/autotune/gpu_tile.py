import os
from functools import reduce

import tvm
import numpy
from tvm import autotvm

from ..codegen.isl_to_tir import build_tvm_stmts, CUDANode2TIRParser
from ..polyhedral.schedule_tree import ScheduleTree


GPU_MAX_THREADS = 512


class GPUTileConfigEntity(list):
    def __init__(self, index, total, *args, **kwargs):
        super(GPUTileConfigEntity, self).__init__(*args, **kwargs)
        self.index = index
        self.total = total

    @property
    def valid(self):
        return True

    def to_json_dict(self):
        return {'index': str(self.index), 'total': str(self.total), 'tile': list(map(str, self))}

    @staticmethod
    def from_json_dict(d):
        return GPUTileConfigEntity(int(d['index']), int(d['total']), list(map(int, d['tile'])))

    def get_flatten_feature(self):
        return numpy.array([self.index / self.total], dtype=numpy.float32)

    def get_other_option(self):
        return {}


class GPUTileConfigSpace(object):
    def __init__(self, n, b):
        super().__init__()
        self.n = n
        self.b = list(b) + [n]
        self.dim = len(self.b) - 1

        self.a = []
        self.l = []
        cur = 1
        while cur <= n:
            self.a.append(cur)
            self.l.append(n // cur - n // (cur + 1))
            if cur >= n:
                break
            cur = n // (n // (cur + 1))
        self.m = len(self.a)
        self.ra = {self.a[i]: i for i in range(self.m)}

        self.ft = [[0 for _ in range(self.m)] for _ in range(self.dim)]
        self.gt = [[[0 for _ in range(self.m)] for _ in range(self.m)] for _ in range(self.dim)]
        for i in range(self.dim):
            for j in range(self.m):
                self.ft[i][j] = self._f(i, self.a[j])
        for i in range(self.dim):
            for j in range(self.m):
                for k in range(self.m):
                    self.gt[i][j][k] = self.g(i, self.a[j], self.a[k])

    @property
    def space_map(self):
        return {'tile': self}

    def get(self, index):
        return GPUTileConfigEntity(index, len(self), self[index])

    def __len__(self):
        return self.size()

    def __iter__(self):
        for i in range(len(self)):
            yield self[i]

    def __getitem__(self, i):
        return self.rev(i)

    def __contains__(self, i):
        return 0 <= i < len(self)

    def size(self):
        return self.f(self.dim - 1, self.n)

    def rank(self, x):
        x = list(x)
        assert len(x) == self.dim
        r = 0
        n = self.n
        for i in range(self.dim - 1, -1, -1):
            r += self.g(i, n, x[i] - 1)
            n //= x[i]
        return r

    def rev(self, x):
        res = [0] * self.dim
        c = x
        n = self.n
        for i in range(self.dim - 1, -1, -1):
            l, r = 1, min(n, self.b[i]) + 1
            while r - l > 1:
                mid = (l + r) // 2
                if self.g(i, n, mid - 1) <= c:
                    l = mid
                else:
                    r = mid
            res[i] = l
            c -= self.g(i, n, l - 1)
            n //= l
        return res

    def g(self, k, n, x):
        assert n in self.ra
        if k < 0 or n <= 0 or x <= 0:
            return 0
        if k <= 0:
            return min(n, self.b[0], x)
        if x <= 1:
            return self.f(k - 1, n)
        q = n // (n // x + 1)
        r = x - q
        return self.gt[k][self.ra[n]][self.ra[q]] + r * self.f(k - 1, n // x)

    def _f(self, k, n):
        if k < 0 or n <= 0:
            return 0
        if k <= 0:
            return min(n, self.b[0])
        bd = self.b[k]
        res, cur = 0, 1
        while cur <= n:
            r = min(n // cur, bd)
            l = n // (cur + 1)
            if r - l >= 1:
                res += (r - l) * self.f(k - 1, cur)
            if cur >= n:
                break
            cur = n // (n // (cur + 1))
        return res

    def bf(self, k, n):
        if k < 0 or n <= 0:
            return 0
        if k <= 0:
            return min(n, self.b[0])
        res = 0
        for i in range(1, min(self.b[k], n) + 1):
            res += self.bf(k - 1, n // i)
        return res

    def f(self, k, n):
        if 0 <= k < self.dim and n in self.ra:
            return self.ft[k][self.ra[n]]
        return self._f(k, n)


class GPUTileTask(autotvm.task.Task):
    def __init__(self, name, tree: ScheduleTree, parser):
        super(GPUTileTask, self).__init__(name, [])
        self.tree = tree
        self.parser = parser
        _, band_size, *_ = tree.parallel_tilable()
        self.config_space = GPUTileConfigSpace(GPU_MAX_THREADS, band_size)
        self.target = tvm.target.create('cuda')

        # TODO: better flop prediction
        self.flop = reduce(float.__mul__, map(float, band_size))

    def instantiate(self, config):
        tree = self.tree.copy()
        tree.gpu_tile(config)
        return build_tvm_stmts(self.name, tree, self.parser)


from ..codegen.isl_to_tir import parser, example_tree
from .builder import PolyLocalBuilder

tree = example_tree.copy()
tree.apply_params(n=512, m=512, q=1024)


def tune_gpu_tile(name: str, tree: ScheduleTree, parser: CUDANode2TIRParser,
         n_trial=40, builder=None, runner=None, tuner=None, callbacks=None) -> ScheduleTree:
    task = GPUTileTask(name, tree.copy(), parser)

    measure_option = {
        'builder': builder or PolyLocalBuilder(),
        'runner': runner or autotvm.LocalRunner(number=6, min_repeat_ms=100, timeout=4),
    }

    tmp_file_name = f'{name}.gpu_tile.log'

    tuner = tuner or autotvm.tuner.XGBTuner(task, feature_type='knob')
    tuner.tune(
        n_trial=n_trial,
        measure_option=measure_option,
        callbacks=[
            autotvm.callback.progress_bar(n_trial, prefix=f'GPUTile {name}'),
            autotvm.callback.log_to_file(tmp_file_name),
            *(callbacks or [])
        ]
    )

    best = autotvm.task.dispatcher.ApplyHistoryBest(tmp_file_name)
    print(best.best_by_targetkey.keys())

    try:
        os.remove(tmp_file_name)
    except Exception as e:
        print(e)


tune_gpu_tile('example', tree, parser)
