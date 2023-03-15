# Owner(s): ["module: functorch"]

import torch
from torch.testing._internal.common_utils import (
    TestCase,
    run_tests,
)

from torch.testing._internal.common_device_type import instantiate_device_type_tests, dtypes
from functorch.compile import aot_function, nop
from torch._decomp.decompositions_for_rng import RNGFunctionalizationError
from unittest.mock import patch
import functools


def count_philox_rand(gm, args, number):
    assert [node.target for node in gm.graph.nodes].count(torch.ops.prims.philox_rand.default) == number
    return gm

class TestFunctionalizationRngOps(TestCase):
    @dtypes(torch.float32)
    @patch.object(torch._functorch.config, "functionalize_rng_ops", True)
    def test_forward(self, dtype, device):
        def fn(x):
            a = torch.rand_like(x) * x
            a = torch.rand_like(x) * a
            return a

        x = torch.rand(10, device=device, dtype=dtype)

        for seed in range(10):
            torch.cuda.manual_seed(seed)
            ref = fn(x)

            torch.cuda.manual_seed(seed)
            aot_fn = aot_function(fn, functools.partial(count_philox_rand, number=2))
            res = aot_fn(x)

            self.assertEqual(ref, res)


    @dtypes(torch.float32)
    @patch.object(torch._functorch.config, "functionalize_rng_ops", True)
    def test_autograd_function(self, dtype, device):
        shape = (16, 16)

        class Custom(torch.autograd.Function):
            @staticmethod
            def forward(ctx, x):
                ctx.save_for_backward(x)
                a = torch.rand_like(x) * x
                return a

            @staticmethod
            def backward(ctx, grad_out):
                x, = ctx.saved_tensors
                return grad_out * torch.rand_like(grad_out) * torch.cos(x)

        custom = Custom.apply

        x = torch.rand(*shape, device=device, dtype=dtype, requires_grad=True)

        x_clone = x.clone().detach().requires_grad_(True)

        torch.cuda.manual_seed(123)
        ref = custom(x)
        ref.sum().backward()

        torch.cuda.manual_seed(123)
        fwd_compiler = functools.partial(count_philox_rand, number=1)
        bwd_compiler = functools.partial(count_philox_rand, number=1)
        aot_custom = aot_function(custom, fwd_compiler, bwd_compiler)
        res = aot_custom(x_clone)
        res.sum().backward()

        self.assertEqual(ref, res)
        self.assertEqual(x.grad, x_clone.grad)

    @dtypes(torch.float32)
    @patch.object(torch._functorch.config, "functionalize_rng_ops", True)
    def test_set_get_rng_state(self, dtype, device):
        def fn(x):
            state = torch.cuda.get_rng_state()
            a = torch.rand_like(x) * x
            a = torch.rand_like(x) * a
            torch.cuda.set_rng_state(state)
            a = torch.rand_like(x) * a
            return a

        x = torch.rand(10, device=device, dtype=dtype)

        for seed in range(10):
            torch.cuda.manual_seed(seed)
            ref = fn(x)

            torch.cuda.manual_seed(seed)
            fwd_compiler = functools.partial(count_philox_rand, number=3)
            aot_fn = aot_function(fn, fwd_compiler)
            res = aot_fn(x)

            self.assertEqual(ref, res)


only_for = ("cuda",)
instantiate_device_type_tests(TestFunctionalizationRngOps, globals(), only_for=only_for)


class NegativeTest(TestCase):
    @dtypes(torch.float32)
    @patch.object(torch._functorch.config, "functionalize_rng_ops", True)
    def test_on_cpu(self, dtype, device):
        def fn(x):
            a = torch.rand_like(x) * x
            a = torch.rand_like(x) * a
            return a

        x = torch.rand(10, device=device, dtype=dtype)

        aot_fn = aot_function(fn, nop)
        try:
            aot_fn(x)
            self.assertTrue(False)
        except RNGFunctionalizationError:
            self.assertTrue(True)


only_for = ("cpu",)
instantiate_device_type_tests(NegativeTest, globals(), only_for=only_for)

if __name__ == "__main__":
    run_tests()
