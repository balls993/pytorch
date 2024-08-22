# Owner(s): ["module: inductor"]

import torch
from functorch import make_fx
from torch._dynamo.utils import counters
from torch._higher_order_ops.auto_functionalize import auto_functionalized
from torch._inductor.fx_passes.reinplace import reinplace_inplaceable_ops_core
from torch._inductor.test_case import run_tests, TestCase as InductorTestCase
from torch.testing._internal.common_utils import IS_LINUX
from torch.testing._internal.inductor_utils import HAS_CUDA


aten = torch.ops.aten


const = torch.tensor(0.0)
device = "cuda"


def num_reinplacing_failures():
    return counters["inductor"]["possibly_missed_reinplacing_opportunities"]


@torch.library.custom_op("_reinplacing::sin", mutates_args={"out"})
def sin(x: torch.Tensor, out: torch.Tensor) -> None:
    out.copy_(x.sin())


@torch.library.custom_op("_reinplacing::sin_cos", mutates_args={"out_sin", "out_cos"})
def sin_cos(x: torch.Tensor, out_sin: torch.Tensor, out_cos: torch.Tensor) -> None:
    out_sin.copy_(x.sin())
    out_cos.copy_(x.cos())


@torch.library.custom_op("test_view::boo", mutates_args={"x"})
def boo(x: torch.Tensor) -> None:
    x.sin_()


class TestReinplacingPassCorrectness(InductorTestCase):
    def setUp(self):
        counters.clear()
        return super().setUp()

    def _test(self, f):
        nf = torch.compile(f)
        inp = (
            torch.randn(4, device=device),
            torch.ones(2, device=device, dtype=torch.int),
        )
        inp2 = (inp[0].clone(), inp[1].clone())
        self.assertEqual(f(*inp), nf(*inp2))
        self.assertEqual(inp, inp2)

    def test_dont_modify_live(self):
        def f(x, y):
            x = x.cos()
            x2 = x.index_put((y,), const)
            return x2, x

        self._test(f)

    def test_dont_modify_view_of_live(self):
        def f(x, y):
            x = x.cos()
            x2 = aten.alias(x)
            x2 = x2.index_put((y,), const)
            y = x2 + x.cos()
            return y

        self._test(f)

    def test_dont_modify_input(self):
        def f(x, y):
            return x.index_put((y,), const)

        self._test(f)

    def test_should_modify_inner(self):
        def f(x, y):
            x = x.cos()
            x = x.index_put((y,), const)
            return x

        self._test(f)

    def test_should_modify_input(self):
        def f(x, y):
            x = x.index_put_((y,), const)
            return x

        self._test(f)

    def test_counters(self):
        counters.clear()

        def f(x):
            out = torch.empty_like(x)
            _, new_out = auto_functionalized(sin._opoverload, x=x, out=out)
            y = out * new_out
            return new_out, y

        x = torch.randn(3, device=device)
        gm = make_fx(f, tracing_mode="fake")(x)
        reinplace_inplaceable_ops_core(gm.graph)

        # We shouldn't have been able to reinplace `out` because it was used after
        # auto_functionalized. Note that this usually doesn't happen in practice;
        # we're artificially creating this example to test the counter.
        # IF THIS NUMBER GOES TO ZERO, PLEASE FIND ANOTHER EXAMPLE
        self.assertEqual(num_reinplacing_failures(), 1)

    def get_clone_count(self, graph):
        counter = 0
        for node in graph.nodes:
            if node.target == torch.ops.higher_order.auto_functionalized:
                counter += len(node.meta["only_clone_these_tensors"])
        return counter

    def test_view_inplaced(self):
        def f(arg0_1):
            select = torch.ops.aten.select.int(arg0_1, 0, 0)
            auto_functionalized = torch.ops.higher_order.auto_functionalized(
                torch.ops.test_view.boo.default,
                x=select,
                _x_base=arg0_1,
                _all_bases=[arg0_1],
            )
            getitem_1 = auto_functionalized[1]
            copy_ = torch.ops.aten.copy_.default(arg0_1, getitem_1)
            return ()

        x1 = torch.randn(3, device=device)
        gm = make_fx(f, tracing_mode="fake")(x1)
        reinplace_inplaceable_ops_core(gm.graph)

        self.assertEqual(self.get_clone_count(gm.graph), 0)

    # introduce a view another_view that is used `after` the copy
    def test_view_inplaced2(self):
        def f(arg0_1):
            select = torch.ops.aten.select.int(arg0_1, 0, 0)
            another_view = arg0_1[2]
            auto_functionalized = torch.ops.higher_order.auto_functionalized(
                torch.ops.test_view.boo.default,
                x=select,
                _x_base=arg0_1,
                _all_bases=[arg0_1],
            )
            getitem_1 = auto_functionalized[1]
            copy_ = torch.ops.aten.copy_.default(arg0_1, getitem_1)
            return another_view

        x1 = torch.randn(3, device=device)
        gm = make_fx(f, tracing_mode="fake")(x1)
        reinplace_inplaceable_ops_core(gm.graph)

        self.assertEqual(self.get_clone_count(gm.graph), 0)

    # introduce a view another_view that is used `before` the copy
    def test_views_not_inplaced(self):
        def f(arg0_1):
            select = torch.ops.aten.select.int(arg0_1, 0, 0)
            another_view = arg0_1[2]
            auto_functionalized = torch.ops.higher_order.auto_functionalized(
                torch.ops.test_view.boo.default,
                x=select,
                _x_base=arg0_1,
                _all_bases=[arg0_1],
            )
            getitem_1 = auto_functionalized[1]
            use_another_view = another_view * 10
            copy_ = torch.ops.aten.copy_.default(arg0_1, getitem_1)
            return use_another_view

        x1 = torch.randn(3, device=device)
        gm = make_fx(f, tracing_mode="fake")(x1)
        reinplace_inplaceable_ops_core(gm.graph)

        self.assertEqual(self.get_clone_count(gm.graph), 1)

    # a view over input with out copy node, inplace not allowed
    def test_views_not_inplaced2(self):
        def f(arg0_1):
            select = torch.ops.aten.select.int(arg0_1, 0, 0)
            another_view = arg0_1[2]
            auto_functionalized = torch.ops.higher_order.auto_functionalized(
                torch.ops.test_view.boo.default,
                x=select,
                _x_base=arg0_1,
                _all_bases=[arg0_1],
            )
            getitem_1 = auto_functionalized[1]
            return

        x1 = torch.randn(3, device=device)
        gm = make_fx(f, tracing_mode="fake")(x1)
        reinplace_inplaceable_ops_core(gm.graph)

        self.assertEqual(self.get_clone_count(gm.graph), 1)

    # no copy nodes, view over local, with a use for another view
    def test_views_not_inplaced3(self):
        def f(arg0_1):
            a = torch.ones(10)
            select = a[0]
            another_view = a[2]
            auto_functionalized = torch.ops.higher_order.auto_functionalized(
                torch.ops.test_view.boo.default,
                x=select,
                _x_base=a,
                _all_bases=[a],
            )
            getitem_1 = auto_functionalized[1]
            return another_view

        x1 = torch.randn(3, device=device)
        gm = make_fx(f, tracing_mode="fake")(x1)
        reinplace_inplaceable_ops_core(gm.graph)

        self.assertEqual(self.get_clone_count(gm.graph), 1)

    def test_multi_output_intermediate(self):
        for requires_grad in [False, True]:
            counters.clear()

            def f(x):
                out1 = torch.empty_like(x)
                out2 = torch.empty_like(x)
                sin_cos(x, out1, out2)
                return out1, out2, x**2

            x = torch.randn(3, device=device, requires_grad=requires_grad)
            res1, res2, _ = torch.compile(f)(x)
            self.assertEqual(res1, x.sin())
            self.assertEqual(res2, x.cos())
            self.assertEqual(num_reinplacing_failures(), 0)

    def test_multiple_mutations(self):
        counters.clear()

        def f(x, out):
            sin(x, out)
            sin(out, out)
            sin(out, out)
            return out

        x = torch.randn(3, device=device)
        out = torch.randn(3, device=device)
        result = torch.compile(f)(x, out)
        self.assertEqual(result, x.sin().sin().sin())
        self.assertEqual(result, out)
        self.assertEqual(num_reinplacing_failures(), 0)

    def test_multiple_intermediate(self):
        counters.clear()

        def f(x):
            out = torch.empty_like(x)
            sin(x, out)
            sin(out, out)
            sin(out, out)
            return out

        x = torch.randn(3, device=device)
        result = torch.compile(f)(x)
        self.assertEqual(result, x.sin().sin().sin())
        self.assertEqual(num_reinplacing_failures(), 0)


if __name__ == "__main__":
    if IS_LINUX and HAS_CUDA:
        run_tests(needs="filelock")
