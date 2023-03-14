# Owner(s): ["module: inductor"]
import contextlib
import importlib
import math
import os
import sys
import unittest
from functools import partial

import torch
from torch._dynamo.testing import make_test_cls_with_patches
from torch.testing._internal.common_device_type import (
    instantiate_device_type_tests,
    onlyCUDA,
)
from torch.testing._internal.common_utils import (
    IS_CI,
    IS_WINDOWS,
    TEST_WITH_ASAN,
    TEST_WITH_ROCM,
    TestCase,
)
from torch.testing._internal.inductor_utils import HAS_CPU, HAS_CUDA

if IS_WINDOWS and IS_CI:
    sys.stderr.write(
        "Windows CI does not have necessary dependencies for test_torchinductor_dynamic_shapes yet\n"
    )
    if __name__ == "__main__":
        sys.exit(0)
    raise unittest.SkipTest("requires sympy/functorch/filelock")

# Make the helper files in test/ importable
pytorch_test_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.append(pytorch_test_dir)
from inductor.test_torchinductor import (
    check_model,
    check_model_cuda,
    CommonTemplate,
    copy_tests,
    TestFailure,
)

importlib.import_module("filelock")

# xfail by default, set is_skip=True to skip
test_failures = {
    "test_cpp_wrapper_dynamic_shapes": TestFailure(("cpu",)),
    "test_kwargs_dynamic_shapes": TestFailure(("cpu",)),
}

if TEST_WITH_ROCM:
    # FIXME: https://github.com/ROCmSoftwarePlatform/frameworks-internal/issues/3849
    test_skips["test_argmax_argmin1_dynamic_shapes"] = ("cpu", "cuda")
    # FIXME: https://github.com/ROCmSoftwarePlatform/frameworks-internal/issues/3462
    test_skips["test_convolution1_dynamic_shapes"] = ("cpu", "cuda")


def make_dynamic_cls(cls):
    return make_test_cls_with_patches(
        cls,
        "DynamicShapes",
        "_dynamic_shapes",
        (torch._dynamo.config, "dynamic_shapes", True),
    )


DynamicShapesCommonTemplate = make_dynamic_cls(CommonTemplate)


if HAS_CPU:

    class DynamicShapesCpuTests(TestCase):
        common = check_model
        device = "cpu"

    copy_tests(DynamicShapesCommonTemplate, DynamicShapesCpuTests, "cpu", test_failures)


if HAS_CUDA and not TEST_WITH_ASAN:

    class DynamicShapesCudaTests(TestCase):
        common = check_model_cuda
        device = "cuda"

    copy_tests(
        DynamicShapesCommonTemplate, DynamicShapesCudaTests, "cuda", test_failures
    )


class TestInductorDynamic(TestCase):
    compile_fn = partial(torch.compile, dynamic=True)

    def setUp(self):
        # HAS_CUDA also checks compute capability to skip tests
        # on older devices
        if self.device_type == "cuda" and not HAS_CUDA:
            self.skipTest("Triton not available")
        torch._dynamo.reset()
        super(TestCase, self).setUp()
        # this should be in setUpClass, but device-generic tests
        # don't work with setUpClass well (non-deterministically the wrong setUpClass is resolved),
        # so put it in test setUp, it's cheap
        self._stack = contextlib.ExitStack()
        self._stack.enter_context(
            torch._inductor.config.patch(
                {
                    "debug": False,
                    "cpp.min_chunk_size": 1,
                    "triton.autotune_pointwise": False,  # too slow
                    "implicit_fallbacks": False,
                }
            )
        )

    def tearDown(self):
        self._stack.close()
        super(TestCase, self).tearDown()
        torch._dynamo.reset()

    def test_arange_dynamic(self, device):
        def fn(a):
            batch_size = a.numel()
            max_len = a.max()
            return ~(
                torch.arange(0, max_len, device=a.device)
                .type_as(a)
                .repeat(batch_size, 1)
                .lt(a.unsqueeze(1))
            )

        a = torch.randint(10, 30, (10,), device=device)
        a[0] = 29  # fix max_len
        opt = self.compile_fn(fn)
        res = opt(a)
        ref = fn(a)
        self.assertEqual(res, ref)

    @onlyCUDA
    def test_pad_dynamic(self, device):
        def get_same_padding(x: int, k: int, s: int, d: int):
            return max((math.ceil(x / s) - 1) * s + (k - 1) * d + 1 - x, 0)

        def pad_same(x, k, s, d=(1, 1), value=0):
            ih, iw = x.size()[-2:]
            pad_h, pad_w = get_same_padding(ih, k[0], s[0], d[0]), get_same_padding(
                iw, k[1], s[1], d[1]
            )
            if pad_h > 0 or pad_w > 0:
                x = torch.nn.functional.pad(
                    x,
                    [pad_w // 2, pad_w - pad_w // 2, pad_h // 2, pad_h - pad_h // 2],
                    value=value,
                )
            return x

        x = torch.randn(2, 24, 110, 110, device=device)
        opt = self.compile_fn(pad_same)
        res = opt(x, (5, 5), (2, 2))
        ref = pad_same(x, (5, 5), (2, 2))
        self.assertEqual(res, ref, atol=0, rtol=0)


instantiate_device_type_tests(TestInductorDynamic, globals())

if __name__ == "__main__":
    from torch._dynamo.test_case import run_tests

    if (HAS_CPU or HAS_CUDA):
        run_tests(needs="filelock")
