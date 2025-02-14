# Copyright 2023 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""PyTests for the AutoGraph source-to-source transformation feature."""

import sys
import traceback
from collections import defaultdict

import jax
import jax.numpy as jnp
import numpy as np
import pennylane as qml
import pytest
from jax.errors import TracerBoolConversionError
from numpy.testing import assert_allclose

from catalyst import (
    adjoint,
    cond,
    ctrl,
    for_loop,
    grad,
    jacobian,
    jvp,
    measure,
    qjit,
    vjp,
)
from catalyst.ag_utils import AutoGraphError, autograph_source, check_cache

# pylint: disable=import-outside-toplevel
# pylint: disable=unnecessary-lambda-assignment
# pylint: disable=too-many-public-methods
# pylint: disable=too-many-lines


class Failing:
    """Test class that emulates failures in user-code"""

    triggered = defaultdict(bool)

    def __init__(self, ref, label: str = "default"):
        self.label = label
        self.ref = ref

    @property
    def val(self):
        """Get a reference to a variable or fail if programmed so."""
        # pylint: disable=broad-exception-raised
        if not Failing.triggered[self.label]:
            Failing.triggered[self.label] = True
            raise Exception(f"Emulated failure with label {self.label}")
        return self.ref


def test_unavailable(monkeypatch):
    """Check the error produced in the absence of tensorflow."""
    monkeypatch.setitem(sys.modules, "tensorflow", None)

    def fn(x):
        return x**2

    with pytest.raises(ImportError, match="AutoGraph feature in Catalyst requires TensorFlow"):
        qjit(autograph=True)(fn)


@pytest.mark.tf
class TestSourceCodeInfo:
    """Unit tests for exception utilities that retrieves traceback information for the original
    source code."""

    def test_non_converted_function(self):
        """Test the robustness of traceback conversion on a non-converted function."""
        from catalyst.ag_primitives import get_source_code_info

        try:
            result = ""
            raise RuntimeError("Test failure")
        except RuntimeError as e:
            result = get_source_code_info(traceback.extract_tb(e.__traceback__, limit=1)[0])

        assert result.split("\n")[1] == '    raise RuntimeError("Test failure")'

    def test_qjit(self):
        """Test source info retrieval for a qjit function."""

        def main():
            for _ in range(5):
                raise RuntimeError("Test failure")
            return 0

        with pytest.warns(
            UserWarning,
            match=(
                f'  File "{__file__}", line [0-9]+, in {main.__name__}\n'
                r"    for _ in range\(5\):"
            ),
        ):
            try:
                qjit(autograph=True)(main)
            except RuntimeError as e:
                assert e.args == ("Test failure",)

    def test_qnode(self):
        """Test source info retrieval for a qnode function."""

        @qml.qnode(qml.device("lightning.qubit", wires=2))
        def main():
            for _ in range(5):
                raise RuntimeError("Test failure")
            return 0

        with pytest.warns(
            UserWarning,
            match=(
                f'  File "{__file__}", line [0-9]+, in {main.__name__}\n'
                r"    for _ in range\(5\):"
            ),
        ):
            try:
                qjit(autograph=True)(main)
            except RuntimeError as e:
                assert e.args == ("Test failure",)

    def test_func(self):
        """Test source info retrieval for a nested function."""

        def inner():
            for _ in range(5):
                raise RuntimeError("Test failure")

        def main():
            inner()
            return 0

        with pytest.warns(
            UserWarning,
            match=(
                f'  File "{__file__}", line [0-9]+, in {inner.__name__}\n'
                r"    for _ in range\(5\):"
            ),
        ):
            try:
                qjit(autograph=True)(main)
            except RuntimeError as e:
                assert e.args == ("Test failure",)


@pytest.mark.tf
class TestIntegration:
    """Test that the autograph transformations trigger correctly in different settings."""

    def test_unsupported_object(self):
        """Check the error produced when attempting to convert an unsupported object (neither of
        QNode, function, or method)."""

        class FN:
            """Test object."""

            def __call__(self, x):
                return x**2

        fn = FN()

        with pytest.raises(AutoGraphError, match="Unsupported object for transformation"):
            qjit(autograph=True)(fn)

    def test_lambda(self):
        """Test autograph on a lambda function."""

        fn = lambda x: x**2
        fn = qjit(autograph=True)(fn)

        assert hasattr(fn.user_function, "ag_unconverted")
        assert check_cache(fn.original_function)
        assert fn(4) == 16

    def test_classical_function(self):
        """Test autograph on a purely classical function."""

        @qjit(autograph=True)
        def fn(x):
            return x**2

        assert hasattr(fn.user_function, "ag_unconverted")
        assert check_cache(fn.original_function)
        assert fn(4) == 16

    def test_nested_function(self):
        """Test autograph on nested classical functions."""

        def inner(x):
            return x**2

        @qjit(autograph=True)
        def fn(x: int):
            return inner(x)

        assert hasattr(fn.user_function, "ag_unconverted")
        assert check_cache(fn.original_function)
        assert check_cache(inner)
        assert fn(4) == 16

    def test_qnode(self):
        """Test autograph on a QNode."""

        @qjit(autograph=True)
        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def fn(x: float):
            qml.RY(x, wires=0)
            return qml.expval(qml.PauliZ(0))

        assert hasattr(fn.user_function, "ag_unconverted")
        assert check_cache(fn.original_function.func)
        assert fn(np.pi) == -1

    def test_indirect_qnode(self):
        """Test autograph on a QNode called from within a classical function."""

        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def inner(x):
            qml.RY(x, wires=0)
            return qml.expval(qml.PauliZ(0))

        @qjit(autograph=True)
        def fn(x: float):
            return inner(x)

        assert hasattr(fn.user_function, "ag_unconverted")
        assert check_cache(fn.original_function)
        assert check_cache(inner.func)
        assert fn(np.pi) == -1

    def test_multiple_qnode(self):
        """Test autograph on multiple QNodes called from different classical functions."""

        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def inner1(x):
            qml.RY(x, wires=0)
            return qml.expval(qml.PauliZ(0))

        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def inner2(x):
            qml.RX(x, wires=0)
            return qml.expval(qml.PauliZ(0))

        @qjit(autograph=True)
        def fn(x: float):
            return inner1(x) + inner2(x)

        assert hasattr(fn.user_function, "ag_unconverted")
        assert check_cache(fn.original_function)
        assert check_cache(inner1.func)
        assert check_cache(inner2.func)
        assert fn(np.pi) == -2

    def test_nested_qnode(self):
        """Test autograph on a QNode called from within another QNode."""

        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def inner1(x):
            qml.RX(x, wires=0)
            return qml.expval(qml.PauliZ(0))

        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def inner2(x):
            y = inner1(x) * np.pi
            qml.RY(y, wires=0)
            return qml.expval(qml.PauliZ(0))

        @qjit(autograph=True)
        def fn(x: int):
            return inner2(x)

        assert hasattr(fn.user_function, "ag_unconverted")
        assert check_cache(fn.original_function)
        assert check_cache(inner1.func)
        assert check_cache(inner2.func)
        # Unsupported by the runtime:
        # assert fn(np.pi) == -2

    def test_nested_qjit(self):
        """Test autograph on a QJIT function called from within the compilation entry point."""

        @qjit
        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def inner(x):
            qml.RY(x, wires=0)
            return qml.expval(qml.PauliZ(0))

        @qjit(autograph=True)
        def fn(x: float):
            return inner(x)

        assert hasattr(fn.user_function, "ag_unconverted")
        assert check_cache(fn.original_function)
        assert check_cache(inner.user_function.func)
        assert fn(np.pi) == -1

    def test_adjoint_wrapper(self):
        """Test conversion is happening succesfully on functions wrapped with 'adjoint'."""

        def inner(x):
            qml.RY(x, wires=0)

        @qjit(autograph=True)
        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def fn(x: float):
            adjoint(inner)(x)
            return qml.probs()

        assert hasattr(fn.user_function, "ag_unconverted")
        assert check_cache(inner)
        assert np.allclose(fn(np.pi), [0.0, 1.0])

    def test_ctrl_wrapper(self):
        """Test conversion is happening succesfully on functions wrapped with 'ctrl'."""

        def inner(x):
            qml.RY(x, wires=0)

        @qjit(autograph=True)
        @qml.qnode(qml.device("lightning.qubit", wires=2))
        def fn(x: float):
            ctrl(inner, control=1)(x)
            return qml.probs()

        assert hasattr(fn.user_function, "ag_unconverted")
        assert check_cache(inner)
        assert np.allclose(fn(np.pi), [1.0, 0.0, 0.0, 0.0])

    def test_grad_wrapper(self):
        """Test conversion is happening succesfully on functions wrapped with 'grad'."""

        def inner(x):
            return 2 * x

        @qjit(autograph=True)
        def fn(x: float):
            return grad(inner)(x)

        assert hasattr(fn.user_function, "ag_unconverted")
        assert check_cache(inner)
        assert fn(3) == 2.0

    def test_jacobian_wrapper(self):
        """Test conversion is happening succesfully on functions wrapped with 'jacobian'."""

        def inner(x):
            return 2 * x, x**2

        @qjit(autograph=True)
        def fn(x: float):
            return jacobian(inner)(x)

        assert hasattr(fn.user_function, "ag_unconverted")
        assert check_cache(inner)
        assert fn(3) == [2.0, 6.0]

    def test_vjp_wrapper(self):
        """Test conversion is happening succesfully on functions wrapped with 'vjp'."""

        def inner(x):
            return 2 * x, x**2

        @qjit(autograph=True)
        def fn(x: float):
            return vjp(inner, (x,), (1.0, 1.0))

        assert hasattr(fn.user_function, "ag_unconverted")
        assert check_cache(inner)
        assert fn(3) == [6.0, 9.0, 8.0]  # unusual vjp return structure, vjp result is 3rd elem

    def test_jvp_wrapper(self):
        """Test conversion is happening succesfully on functions wrapped with 'jvp'."""

        def inner(x):
            return 2 * x, x**2

        @qjit(autograph=True)
        def fn(x: float):
            return jvp(inner, (x,), (1.0,))

        assert hasattr(fn.user_function, "ag_unconverted")
        assert check_cache(inner)
        assert fn(3) == [6.0, 9.0, 2.0, 6.0]  # unusual jvp return structure, jvp results start 3rd


@pytest.mark.tf
class TestCodePrinting:
    """Test that the transformed source code can be printed in different settings."""

    def test_unconverted(self):
        """Test printing on an unconverted function."""

        @qjit(autograph=False)
        def fn(x):
            return x**2

        with pytest.raises(AutoGraphError, match="function was not converted by AutoGraph"):
            autograph_source(fn)

    def test_lambda(self):
        """Test printing on a lambda function."""

        fn = lambda x: x**2
        qjit(autograph=True)(fn)

        assert autograph_source(fn)

    def test_classical_function(self):
        """Test printing on a purely classical function."""

        @qjit(autograph=True)
        def fn(x):
            return x**2

        assert autograph_source(fn)

    def test_nested_function(self):
        """Test printing on nested classical functions."""

        def inner(x):
            return x**2

        @qjit(autograph=True)
        def fn(x: int):
            return inner(x)

        assert autograph_source(fn)
        assert autograph_source(inner)

    def test_qnode(self):
        """Test printing on a QNode."""

        @qjit(autograph=True)
        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def fn(x: float):
            qml.RY(x, wires=0)
            return qml.expval(qml.PauliZ(0))

        assert autograph_source(fn)

    def test_indirect_qnode(self):
        """Test printing on a QNode called from within a classical function."""

        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def inner(x):
            qml.RY(x, wires=0)
            return qml.expval(qml.PauliZ(0))

        @qjit(autograph=True)
        def fn(x: float):
            return inner(x)

        assert autograph_source(fn)
        assert autograph_source(inner)

    def test_multiple_qnode(self):
        """Test printing on multiple QNodes called from different classical functions."""

        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def inner1(x):
            qml.RY(x, wires=0)
            return qml.expval(qml.PauliZ(0))

        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def inner2(x):
            qml.RX(x, wires=0)
            return qml.expval(qml.PauliZ(0))

        @qjit(autograph=True)
        def fn(x: float):
            return inner1(x) + inner2(x)

        assert autograph_source(fn)
        assert autograph_source(inner1)
        assert autograph_source(inner2)

    def test_nested_qnode(self):
        """Test printing on a QNode called from within another QNode."""

        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def inner1(x):
            qml.RX(x, wires=0)
            return qml.expval(qml.PauliZ(0))

        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def inner2(x):
            y = inner1(x) * np.pi
            qml.RY(y, wires=0)
            return qml.expval(qml.PauliZ(0))

        @qjit(autograph=True)
        def fn(x: int):
            return inner2(x)

        assert autograph_source(fn)
        assert autograph_source(inner1)
        assert autograph_source(inner2)

    def test_nested_qjit(self):
        """Test printing on a QJIT function called from within the compilation entry point."""

        @qjit
        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def inner(x):
            qml.RY(x, wires=0)
            return qml.expval(qml.PauliZ(0))

        @qjit(autograph=True)
        def fn(x: float):
            return inner(x)

        assert autograph_source(fn)
        assert autograph_source(inner)


@pytest.mark.tf
class TestConditionals:
    """Test that the autograph transformations produce correct results on conditionals.
    These tests are adapted from the test_conditionals.TestCond class of tests."""

    def test_simple_cond(self):
        """Test basic function with conditional."""

        @qjit(autograph=True)
        def circuit(n):
            if n > 4:
                res = n**2
            else:
                res = n

            return res

        assert circuit(0) == 0
        assert circuit(1) == 1
        assert circuit(2) == 2
        assert circuit(3) == 3
        assert circuit(4) == 4
        assert circuit(5) == 25
        assert circuit(6) == 36

    def test_cond_one_else_if(self):
        """Test a cond with one else_if branch"""

        @qjit(autograph=True)
        def circuit(x):
            if x > 2.7:
                res = x * 4
            elif x > 1.4:
                res = x * 2
            else:
                res = x

            return res

        assert circuit(4) == 16
        assert circuit(2) == 4
        assert circuit(1) == 1

    def test_cond_many_else_if(self):
        """Test a cond with multiple else_if branches"""

        @qjit(autograph=True)
        def circuit(x):
            if x > 4.8:
                res = x * 8
            elif x > 2.7:
                res = x * 4
            elif x > 1.4:
                res = x * 2
            else:
                res = x

            return res

        assert circuit(5) == 40
        assert circuit(3) == 12
        assert circuit(2) == 4
        assert circuit(-3) == -3

    def test_qubit_manipulation_cond(self, backend):
        """Test conditional with quantum operation."""

        @qjit(autograph=True)
        @qml.qnode(qml.device(backend, wires=1))
        def circuit(x):
            if x > 4:
                qml.PauliX(wires=0)

            return measure(wires=0)

        # pylint: disable=singleton-comparison
        assert circuit(3) == False
        assert circuit(6) == True

    def test_branch_return_mismatch(self, backend):
        """Test that an exception is raised when the true branch returns a value without an else
        branch.
        """
        # pylint: disable=using-constant-test

        def circuit():
            if True:
                res = measure(wires=0)

            return res

        with pytest.raises(
            AutoGraphError, match="Some branches did not define a value for variable 'res'"
        ):
            qjit(autograph=True)(qml.qnode(qml.device(backend, wires=1))(circuit))

    def test_branch_no_multi_return_mismatch(self, backend):
        """Test that case when the return types of all branches do not match."""
        # pylint: disable=using-constant-test

        @qjit(autograph=True)
        @qml.qnode(qml.device(backend, wires=1))
        def circuit():
            if True:
                res = measure(wires=0)
            elif False:
                res = 0.0
            else:
                res = measure(wires=0)

            return res

        assert 0.0 == circuit()


@pytest.mark.tf
class TestForLoops:
    """Test that the autograph transformations produce correct results on for loops."""

    def test_python_range_fallback(self):
        """Test that the custom CRange wrapper correctly falls back to Python."""
        from catalyst.ag_primitives import CRange

        # pylint: disable=protected-access

        c_range = CRange(0, 5, 1)
        assert c_range._py_range is None

        assert isinstance(c_range.py_range, range)  # automatically instantiates the Python range
        assert isinstance(c_range._py_range, range)
        assert c_range[2] == 2

    def test_for_in_array(self):
        """Test for loop over JAX array."""

        @qjit(autograph=True)
        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def f(params):
            for x in params:
                qml.RY(x, wires=0)
            return qml.expval(qml.PauliZ(0))

        result = f(jnp.array([0.0, 1 / 4 * jnp.pi, 2 / 4 * jnp.pi]))
        assert np.allclose(result, -jnp.sqrt(2) / 2)

    def test_for_in_array_unpack(self):
        """Test for loop over a 2D JAX array unpacking the inner dimension."""

        @qjit(autograph=True)
        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def f(params):
            for x1, x2 in params:
                qml.RY(x1, wires=0)
                qml.RY(x2, wires=0)
            return qml.expval(qml.PauliZ(0))

        result = f(jnp.array([[0.0, 1 / 4 * jnp.pi], [2 / 4 * jnp.pi, jnp.pi]]))
        assert np.allclose(result, jnp.sqrt(2) / 2)

    def test_for_in_numeric_list(self):
        """Test for loop over a Python list that is convertible to an array."""

        @qjit(autograph=True)
        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def f():
            params = [0.0, 1 / 4 * jnp.pi, 2 / 4 * jnp.pi]
            for x in params:
                qml.RY(x, wires=0)
            return qml.expval(qml.PauliZ(0))

        result = f()
        assert np.allclose(result, -jnp.sqrt(2) / 2)

    def test_for_in_numeric_list_of_list(self):
        """Test for loop over a nested Python list that is convertible to an array."""

        @qjit(autograph=True)
        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def f():
            params = [[0.0, 1 / 4 * jnp.pi], [2 / 4 * jnp.pi, jnp.pi]]
            for xx in params:
                for x in xx:
                    qml.RY(x, wires=0)
            return qml.expval(qml.PauliZ(0))

        result = f()
        assert np.allclose(result, jnp.sqrt(2) / 2)

    def test_for_in_object_list(self):
        """Test for loop over a Python list that is *not* convertible to an array.
        The behaviour should fall back to standard Python."""

        @qjit(autograph=True)
        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def f():
            params = ["0", "1", "2"]
            for x in params:
                qml.RY(int(x) / 4 * jnp.pi, wires=0)
            return qml.expval(qml.PauliZ(0))

        result = f()
        assert np.allclose(result, -jnp.sqrt(2) / 2)

    def test_for_in_object_list_strict(self, monkeypatch):
        """Check the error raised in strict mode when a for loop iterates over a Python list that
        is *not* convertible to an array."""
        monkeypatch.setattr("catalyst.autograph_strict_conversion", True)

        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def f():
            params = ["0", "1", "2"]
            for x in params:
                qml.RY(int(x) / 4 * jnp.pi, wires=0)
            return qml.expval(qml.PauliZ(0))

        with pytest.raises(AutoGraphError, match="Could not convert the iteration target"):
            qjit(autograph=True)(f)

    def test_for_in_static_range(self):
        """Test for loop over a Python range with static bounds."""

        @qjit(autograph=True)
        @qml.qnode(qml.device("lightning.qubit", wires=3))
        def f():
            for i in range(3):
                qml.Hadamard(i)
            return qml.probs()

        result = f()
        assert np.allclose(result, [1 / 8] * 8)

    def test_for_in_static_range_indexing_array(self):
        """Test for loop over a Python range with static bounds that is used to index an array."""

        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def f():
            params = jnp.array([0.0, 1 / 4 * jnp.pi, 2 / 4 * jnp.pi])
            for i in range(3):
                qml.RY(params[i], wires=0)
            return qml.expval(qml.PauliZ(0))

        result = f()
        assert np.allclose(result, -jnp.sqrt(2) / 2)

    # With conversion always taking place, the user needs to be careful to manually wrap
    # objects accessed via loop iteration indices into arrays (see test case above).
    # The warning here is actionable.
    def test_for_in_static_range_indexing_numeric_list(self):
        """Test for loop over a Python range with static bounds that is used to index an
        array-compatible Python list. This should fall back to Python with a warning."""

        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def f():
            params = [0.0, 1 / 4 * jnp.pi, 2 / 4 * jnp.pi]
            for i in range(3):
                qml.RY(params[i], wires=0)
            return qml.expval(qml.PauliZ(0))

        with pytest.warns(
            match=r"TracerIntegerConversionError:    The __index__\(\) method was called"
        ):
            qjit(autograph=True)(f)

    # This case is slightly problematic because there is no way for the user to compile this for
    # loop correctly. Fallback to a Python loop is always necessary, and will result in a warning.
    # The warning here is not actionable.
    def test_for_in_static_range_indexing_object_list(self):
        """Test for loop over a Python range with static bounds that is used to index an
        array-incompatible Python list. This should fall back to Python with a warning."""

        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def f():
            params = ["0", "1", "2"]
            for i in range(3):
                qml.RY(int(params[i]) / 4 * jnp.pi, wires=0)
            return qml.expval(qml.PauliZ(0))

        with pytest.warns(
            match=r"TracerIntegerConversionError:    The __index__\(\) method was called"
        ):
            qjit(autograph=True)(f)

    def test_for_in_dynamic_range(self):
        """Test for loop over a Python range with dynamic bounds."""

        @qjit(autograph=True)
        @qml.qnode(qml.device("lightning.qubit", wires=3))
        def f(n: int):
            for i in range(n):
                qml.Hadamard(i)
            return qml.probs()

        result = f(3)
        assert np.allclose(result, [1 / 8] * 8)

    def test_for_in_dynamic_range_indexing_array(self):
        """Test for loop over a Python range with dynamic bounds that is used to index an array."""

        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def f(n: int):
            params = jnp.array([0.0, 1 / 4 * jnp.pi, 2 / 4 * jnp.pi])
            for i in range(n):
                qml.RY(params[i], wires=0)
            return qml.expval(qml.PauliZ(0))

        result = f(3)
        assert np.allclose(result, -jnp.sqrt(2) / 2)

    # This case will fail even without autograph conversion, since dynamic iteration bounds are not
    # allowed in Python ranges. Here, AutoGraph improves the situation by allowing this test case
    # with a slight modification of the user code (see test case above).
    # Raising the warning is vital here to notify the user that this use case is actually supported,
    # but requires a modification. Without it, the user may simply conclude it is unsupported.
    def test_for_in_dynamic_range_indexing_numeric_list(self):
        """Test for loop over a Python range with dynamic bounds that is used to index an
        array-compatible Python list. The fallback to Python will first raise a warning,
        then an error."""

        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def f(n: int):
            params = [0.0, 1 / 4 * jnp.pi, 2 / 4 * jnp.pi]
            for i in range(n):
                qml.RY(params[i], wires=0)
            return qml.expval(qml.PauliZ(0))

        with pytest.warns(
            match=r"TracerIntegerConversionError:    The __index__\(\) method was called"
        ):
            with pytest.raises(jax.errors.TracerIntegerConversionError, match="__index__"):
                qjit(autograph=True)(f)

    # This use case is never possible, regardless of whether AutoGraph is used or not.
    def test_for_in_dynamic_range_indexing_object_list(self):
        """Test for loop over a Python range with dynamic bounds that is used to index an
        array-incompatible Python list. The fallback to Python will first raise a warning,
        then an error."""

        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def f(n: int):
            params = ["0", "1", "2"]
            for i in range(n):
                qml.RY(int(params[i]) * jnp.pi, wires=0)
            return qml.expval(qml.PauliZ(0))

        with pytest.warns(
            match=r"TracerIntegerConversionError:    The __index__\(\) method was called"
        ):
            with pytest.raises(jax.errors.TracerIntegerConversionError, match="__index__"):
                qjit(autograph=True)(f)

    def test_for_in_enumerate_array(self):
        """Test for loop over a Python enumeration on an array."""

        @qjit(autograph=True)
        @qml.qnode(qml.device("lightning.qubit", wires=3))
        def f(params):
            for i, x in enumerate(params):
                qml.RY(x, wires=i)
            return [qml.expval(qml.PauliZ(i)) for i in range(3)]

        result = f(jnp.array([0.0, 1 / 4 * jnp.pi, 2 / 4 * jnp.pi]))
        assert np.allclose(result, [1.0, jnp.sqrt(2) / 2, 0.0])

    def test_for_in_enumerate_array_no_unpack(self):
        """Test for loop over a Python enumeration with delayed unpacking."""

        @qjit(autograph=True)
        @qml.qnode(qml.device("lightning.qubit", wires=3))
        def f(params):
            for v in enumerate(params):
                qml.RY(v[1], wires=v[0])
            return [qml.expval(qml.PauliZ(i)) for i in range(3)]

        result = f(jnp.array([0.0, 1 / 4 * jnp.pi, 2 / 4 * jnp.pi]))
        assert np.allclose(result, [1.0, jnp.sqrt(2) / 2, 0.0])

    def test_for_in_enumerate_nested_unpack(self):
        """Test for loop over a Python enumeration with nested unpacking."""

        @qjit(autograph=True)
        @qml.qnode(qml.device("lightning.qubit", wires=3))
        def f(params):
            for i, (x1, x2) in enumerate(params):
                qml.RY(x1, wires=i)
                qml.RY(x2, wires=i)
            return [qml.expval(qml.PauliZ(i)) for i in range(3)]

        result = f(
            jnp.array(
                [[0.0, 1 / 4 * jnp.pi], [2 / 4 * jnp.pi, 3 / 4 * jnp.pi], [jnp.pi, 2 * jnp.pi]]
            )
        )
        assert np.allclose(result, [jnp.sqrt(2) / 2, -jnp.sqrt(2) / 2, -1.0])

    def test_for_in_enumerate_start(self):
        """Test for loop over a Python enumeration with offset indices."""

        @qjit(autograph=True)
        @qml.qnode(qml.device("lightning.qubit", wires=5))
        def f(params):
            for i, x in enumerate(params, start=2):
                qml.RY(x, wires=i)
            return [qml.expval(qml.PauliZ(i)) for i in range(5)]

        result = f(jnp.array([0.0, 1 / 4 * jnp.pi, 2 / 4 * jnp.pi]))
        assert np.allclose(result, [1.0, 1.0, 1.0, jnp.sqrt(2) / 2, 0.0])

    def test_for_in_enumerate_numeric_list(self):
        """Test for loop over a Python enumeration on a list that is convertible to an array."""

        @qjit(autograph=True)
        @qml.qnode(qml.device("lightning.qubit", wires=3))
        def f():
            params = [0.0, 1 / 4 * jnp.pi, 2 / 4 * jnp.pi]
            for i, x in enumerate(params):
                qml.RY(x, wires=i)
            return [qml.expval(qml.PauliZ(i)) for i in range(3)]

        result = f()
        assert np.allclose(result, [1.0, jnp.sqrt(2) / 2, 0.0])

    def test_for_in_enumerate_object_list(self):
        """Test for loop over a Python enumeration on a list that is *not* convertible to an array.
        The behaviour should fall back to standard Python."""

        @qjit(autograph=True)
        @qml.qnode(qml.device("lightning.qubit", wires=3))
        def f():
            params = ["0", "1", "2"]
            for i, x in enumerate(params):
                qml.RY(int(x) / 4 * jnp.pi, wires=i)
            return [qml.expval(qml.PauliZ(i)) for i in range(3)]

        result = f()
        assert np.allclose(result, [1.0, jnp.sqrt(2) / 2, 0.0])

    def test_for_in_other_iterable_object(self):
        """Test for loop over arbitrary iterable Python objects.
        The behaviour should fall back to standard Python."""

        @qjit(autograph=True)
        @qml.qnode(qml.device("lightning.qubit", wires=1))
        def f():
            params = {"a": 0.0, "b": 1 / 4 * jnp.pi, "c": 2 / 4 * jnp.pi}
            for k, v in params.items():
                print(k)
                qml.RY(v, wires=0)
            return qml.expval(qml.PauliZ(0))

        result = f()
        assert np.allclose(result, -jnp.sqrt(2) / 2)

    def test_loop_carried_value(self, monkeypatch):
        """Test a loop which updates a value each iteration."""
        monkeypatch.setattr("catalyst.autograph_strict_conversion", True)

        @qjit(autograph=True)
        def f1():
            acc = 0
            for x in [0, 4, 5]:
                acc = acc + x

            return acc

        assert f1() == 9

        @qjit(autograph=True)
        def f2(acc):
            for x in [0, 4, 5]:
                acc = acc + x

            return acc

        assert f2(2) == 11

        @qjit(autograph=True)
        def f3():
            acc = 0
            for x in [0, 4, 5]:
                acc += x

            return acc

        assert f3() == 9

    def test_iteration_element_access(self, monkeypatch):
        """Test that access to the iteration index/elements is possible after the loop executed
        (assuming initialization)."""
        monkeypatch.setattr("catalyst.autograph_strict_conversion", True)

        @qjit(autograph=True)
        def f1(acc):
            x = 0
            for x in [0, 4, 5]:
                acc = acc + x
            ...  # use acc

            return x

        assert f1(0) == 5

        @qjit(autograph=True)
        def f2(acc):
            i = 0
            l = jnp.array([0, 4, 5])
            for i in range(3):
                acc = acc + l[i]
            ...  # use acc

            return i

        assert f2(0) == 2

        @qjit(autograph=True)
        def f3(acc):
            i, x = 0, 0
            for i, x in enumerate([0, 4, 5]):
                acc = acc + x
            ...  # use acc

            return i, x

        assert f3(0) == (2, 5)

    @pytest.mark.xfail(reason="currently unsupported, but we may find a way to do so in the future")
    def test_iteration_element_access_no_init(self, monkeypatch):
        """Test that access to the iteration index/elements is possible after the loop executed
        even without prior initialization."""
        monkeypatch.setattr("catalyst.autograph_strict_conversion", True)

        @qjit(autograph=True)
        def f1(acc):
            for x in [0, 4, 5]:
                acc = acc + x
            ...  # use acc

            return x

        assert f1(0) == 5

        @qjit(autograph=True)
        def f2(acc):
            l = jnp.array([0, 4, 5])
            for i in range(3):
                acc = acc + l[i]
            ...  # use acc

            return i

        assert f2(0) == 2

        @qjit(autograph=True)
        def f3(acc):
            for i, x in enumerate([0, 4, 5]):
                acc = acc + x
            ...  # use acc

            return i, x

        assert f3(0) == (2, 5)

    def test_temporary_loop_variable(self, monkeypatch):
        """Test that temporary (local) variables can be initialized inside a loop."""
        monkeypatch.setattr("catalyst.autograph_strict_conversion", True)

        @qjit(autograph=True)
        def f1():
            acc = 0
            for x in [0, 4, 5]:
                c = 2
                acc = acc + c * x

            return acc

        assert f1() == 18

        @qjit(autograph=True)
        def f2():
            acc = 0
            for x in [0, 4, 5]:
                c = x * 2
                acc = acc + c

            return acc

        assert f2() == 18

    def test_uninitialized_variables(self, monkeypatch):
        """Verify errors for (potentially) uninitialized loop variables."""
        monkeypatch.setattr("catalyst.autograph_strict_conversion", True)

        def f1():
            for x in [0, 4, 5]:
                acc = acc + x

            return acc

        with pytest.raises(AutoGraphError, match="'acc' is potentially uninitialized"):
            qjit(autograph=True)(f1)

        def f2():
            acc = 0
            for x in [0, 4, 5]:
                acc = acc + x

            return x

        with pytest.raises(AutoGraphError, match="'x' is potentially uninitialized"):
            qjit(autograph=True)(f2)

        def f3():
            acc = 0
            for x in [0, 4, 5]:
                c = 2
                acc = acc + c * x

            return c

        with pytest.raises(AutoGraphError, match="'c' is potentially uninitialized"):
            qjit(autograph=True)(f3)

    def test_init_with_invalid_jax_type(self, monkeypatch):
        """Test loop carried values initialized with an invalid JAX type."""
        monkeypatch.setattr("catalyst.autograph_strict_conversion", True)

        def f():
            acc = 0
            x = ""
            for x in [0, 4, 5]:
                acc = acc + x

            return x

        with pytest.raises(AutoGraphError, match="'x' was initialized with type <class 'str'>"):
            qjit(autograph=True)(f)

    def test_init_with_mismatched_type(self, monkeypatch):
        """Test loop carried values initialized with a mismatched type compared to the values used
        inside the loop."""
        monkeypatch.setattr("catalyst.autograph_strict_conversion", True)

        def f():
            acc = 0
            x = 0.0
            for x in [0, 4, 5]:
                acc = acc + x

            return x

        with pytest.raises(AutoGraphError, match="'x' was initialized with the wrong type"):
            qjit(autograph=True)(f)

    @pytest.mark.filterwarnings("error")
    def test_ignore_warnings(self, monkeypatch):
        """Test the AutoGraph config flag properly silences warnings."""
        monkeypatch.setattr("catalyst.autograph_ignore_fallbacks", True)

        @qjit(autograph=True)
        def f():
            acc = 0
            data = [0, 4, 5]
            for i in range(3):
                acc = acc + data[i]

            return acc

        assert f() == 9


@pytest.mark.tf
class TestWhileLoops:
    """Test that the autograph transformations produce correct results on while loops."""

    @pytest.mark.parametrize(
        "init,inc,expected", [(0, 1, 3), (0.0, 1.0, 3.0), (0.0 + 0j, 1.0 + 0j, 3.0 + 0j)]
    )
    def test_whileloop_basic(self, monkeypatch, init, inc, expected):
        """Test basic while-loop functionality"""
        monkeypatch.setattr("catalyst.autograph_strict_conversion", True)

        @qjit(autograph=True)
        def f(limit):
            i = init
            while i < limit:
                i += inc
            return i

        result = f(expected)
        assert result == expected

    def test_whileloop_multiple_variables(self, monkeypatch):
        """Test while-loop with a multiple state variables"""
        monkeypatch.setattr("catalyst.autograph_strict_conversion", True)

        @qjit(autograph=True)
        def f(param):
            a = 0
            b = 0
            while a < param:
                a += 1
                b += 1
            return b

        result = f(3)
        assert result == 3

    def test_whileloop_qjit(self, monkeypatch):
        """Test while-loop used with qml calls"""
        monkeypatch.setattr("catalyst.autograph_strict_conversion", True)

        @qjit(autograph=True)
        @qml.qnode(qml.device("lightning.qubit", wires=4))
        def f(p):
            w = int(0)
            while w < 4:
                qml.RY(p, wires=w)
                p *= 0.5
                w += 1
            return qml.probs()

        result = f(2.0**4)
        expected = jnp.array(
            # fmt:off
            [
                0.00045727, 0.00110912, 0.0021832, 0.0052954,
                0.000613, 0.00148684, 0.00292669, 0.00709874,
                0.02114249, 0.0512815, 0.10094267, 0.24483834,
                0.02834256, 0.06874542, 0.13531871, 0.32821807,
            ]
            # fmt:on
        )
        assert_allclose(result, expected, rtol=1e-6, atol=1e-6)

    def test_whileloop_temporary_variable(self, monkeypatch):
        """Test that temporary (local) variables can be initialized inside a while loop."""
        monkeypatch.setattr("catalyst.autograph_strict_conversion", True)

        @qjit(autograph=True)
        def f1():
            acc = 0
            while acc < 3:
                c = 2
                acc = acc + c

            return acc

        assert f1() == 4

    def test_whileloop_forloop_interop(self, monkeypatch):
        """Test for-loop co-existing with while loop."""
        monkeypatch.setattr("catalyst.autograph_strict_conversion", True)

        @qjit(autograph=True)
        def f1():
            acc = 0
            while acc < 5:
                acc = acc + 1
                for x in [1, 2, 3]:
                    acc += x
            return acc

        assert f1() == 0 + 1 + sum([1, 2, 3])

    def test_whileloop_cond_interop(self, monkeypatch):
        """Test for-loop co-existing with while loop."""
        monkeypatch.setattr("catalyst.autograph_strict_conversion", True)

        @qjit(autograph=True)
        def f1():
            acc = 0
            while acc < 5:
                if acc < 2:
                    acc += 1
                else:
                    acc += 2
            return acc

        assert f1() == sum([1, 1, 2, 2])

    @pytest.mark.xfail(reason="this won't run warning-free until we fix the resource warning issue")
    @pytest.mark.filterwarnings("error")
    def test_whileloop_no_warning(self, monkeypatch):
        """Test the absence of warnings if fallbacks are ignored."""
        monkeypatch.setattr("catalyst.autograph_ignore_fallbacks", True)

        @qjit(autograph=True)
        def f():
            acc = 0
            while Failing(acc).val < 5:
                acc = acc + 1
            return acc

        assert f() == 5

    def test_whileloop_exception(self, monkeypatch):
        """Test for-loop error if strict-conversion is enabled."""
        monkeypatch.setattr("catalyst.autograph_strict_conversion", True)

        def f1():
            acc = 0
            while acc < 5:
                raise RuntimeError("Test failure")
            return acc

        with pytest.raises(RuntimeError):
            qjit(autograph=True)(f1)()

    def test_uninitialized_variables(self, monkeypatch):
        """Verify errors for (potentially) uninitialized loop variables."""
        monkeypatch.setattr("catalyst.autograph_strict_conversion", True)

        def f(pred: bool):
            while pred:
                x = 3

            return x

        with pytest.raises(AutoGraphError, match="'x' is potentially uninitialized"):
            qjit(autograph=True)(f)

    def test_init_with_invalid_jax_type(self, monkeypatch):
        """Test loop carried values initialized with an invalid JAX type."""
        monkeypatch.setattr("catalyst.autograph_strict_conversion", True)

        def f(pred: bool):
            x = ""

            while pred:
                x = 3

            return x

        with pytest.raises(AutoGraphError, match="'x' was initialized with type <class 'str'>"):
            qjit(autograph=True)(f)

    def test_init_with_mismatched_type(self, monkeypatch):
        """Test loop carried values initialized with a mismatched type compared to the values used
        inside the loop."""
        monkeypatch.setattr("catalyst.autograph_strict_conversion", True)

        def f(pred: bool):
            x = 0.0

            while pred:
                x = 3

            return x

        with pytest.raises(AutoGraphError, match="'x' was initialized with the wrong type"):
            qjit(autograph=True)(f)


@pytest.mark.tf
@pytest.mark.parametrize(
    "execution_context", (lambda fn: fn, qml.qnode(qml.device("lightning.qubit", wires=1)))
)
class TestFallback:
    """Test that Python fallbacks still produce correct results."""

    def test_postbinding_errors_for(self, execution_context):
        """Test that errors are handled correctly if they trigger after the JAX primitive binding
        step (e.g. during result verification), and that no errors occur after the AG tracing step
        (e.g. during lowering) because of malformed primitives in the JAXPR.
        This test ensures the primitive identification and removal works correctly on fallback. In
        this case, the loop primitive should be removed since the exception happens after binding.
        """

        @execution_context
        def f():
            arr = jnp.array([1, 2])
            for _ in range(2):
                # fails result verification, will trigger fallback
                # would raise an error during lowering if left in the JAXPR
                arr = jnp.kron(arr, arr)
            return arr

        with pytest.warns(
            UserWarning, match="Tracing of an AutoGraph converted for loop failed with an exception"
        ):
            f_jit = qjit(autograph=True)(f)

        arr = jnp.array([1, 2])
        expected = jnp.kron(*([jnp.kron(arr, arr)] * 2))
        assert np.allclose(f_jit(), expected)

    def test_prebinding_errors_for(self, execution_context):
        """Test that errors are handled correctly if they trigger before the JAX primitive binding
        step (e.g. during argument verification).
        This test ensures the primitive identification and removal works correctly on fallback. In
        this case no primitive should be removed since the exception happens before binding.
        """

        @execution_context
        def f():
            string = "hi"
            arr = jnp.array([1, 2])
            for _ in range(2):
                arr = arr + 3
            for i in range(1, 4):
                string = string * i  # fails return type verification, triggers fallback
            return arr, len(string)

        with pytest.warns(
            UserWarning, match="Tracing of an AutoGraph converted for loop failed with an exception"
        ):
            f_jit = qjit(autograph=True)(f)

        results = f_jit()
        assert np.allclose(results[0], [7, 8])
        assert results[1] == (2) * 1 * 2 * 3  # i = range(1, 4)

    def test_postbinding_errors_while(self, execution_context):
        """Test that errors are handled correctly if they trigger after the JAX primitive binding
        step (e.g. during result verification), and that no errors occur after the AG tracing step
        (e.g. during lowering) because of malformed primitives in the JAXPR.
        This test ensures the primitive identification and removal works correctly on fallback. In
        this case, the loop primitive should be removed since the exception happens after binding.
        """

        @qjit(autograph=True)
        @execution_context
        def f():
            arr = jnp.array([1, 2])
            while len(arr) < 16:
                # fails result verification, will trigger fallback
                # would raise an error during lowering if left in the JAXPR
                arr = jnp.kron(arr, arr)
            return arr

        arr = jnp.array([1, 2])
        result = f()
        expected = jnp.kron(*([jnp.kron(arr, arr)] * 2))
        assert np.allclose(result, expected)

    def test_prebinding_errors_while(self, execution_context):
        """Test that errors are handled correctly if they trigger before the JAX primitive binding
        step (e.g. during argument verification).
        This test ensures the primitive identification and removal works correctly on fallback. In
        this case no primitive should be removed since the exception happens before binding.
        """

        @qjit(autograph=True)
        @execution_context
        def f():
            string = "hi"
            arr = jnp.array([1, 2])
            i = 1

            while arr[0] < 7:
                arr = arr + 3

            while len(string) < 12:
                string = string * i  # fails return type verification, triggers fallback
                i += 1

            return arr, len(string)

        results = f()
        assert np.allclose(results[0], [7, 8])
        assert results[1] == (2) * 1 * 2 * 3  # i = range(1, 4)


@pytest.mark.tf
class TestLogicalOps:
    """Test logical operations: and, or, not"""

    def test_logical_basics(self):
        """Test basic logical and behavior."""
        # pylint: disable=chained-comparison

        def f1(param):
            return param > 0.0 and param < 1.0 and param <= 2.0

        def f2(param):
            return param > 1.0 or param < 0.0 or param == 0.5

        def f3(param):
            return not param > 1.0

        assert qjit(autograph=True)(f1)(0.5) == np.array(True)
        assert qjit(autograph=True)(f2)(0.5) == np.array(True)
        assert qjit(autograph=True)(f3)(0.5) == np.array(True)

    # fmt:off
    @pytest.mark.parametrize("python_object",["string", [0, 1, 2], [], {1: 2}, {}, ],)
    # fmt:on
    def test_logical_with_python_objects(self, python_object):
        """Test that logical ops still work with python objects."""

        @qjit(autograph=True)
        def f():
            r1 = True and python_object
            assert r1 is python_object
            r2 = False or python_object
            assert r2 is python_object
            r3 = not python_object
            assert isinstance(r3, bool)
            return 1

        assert 1 == f()

    def test_logical_accepts_non_scalars(self):
        """Test that we accept logic with non-scalar tensors if both are traced"""

        def f_and(a, b):
            return a and b

        def f_or(a, b):
            return a or b

        def f_not(a):
            return not a

        a, b = jnp.array([0, 1]), jnp.array([1, 1])
        assert_allclose(qjit(autograph=True)(f_and)(a, b), jnp.logical_and(a, b))
        assert_allclose(qjit(autograph=True)(f_or)(a, b), jnp.logical_or(a, b))
        assert_allclose(qjit(autograph=True)(f_not)(a), jnp.logical_not(a))

    @pytest.mark.parametrize("s,d", [(True, True), (True, False), (False, True), (False, False)])
    def test_logical_mixture_static_dynamic_default(self, s, d):
        """Test the useage of a mixture of static(s) and dynamic(d) variables."""

        # Here we either return bool or the dynamic object
        assert qjit(autograph=True)(lambda d: s and d)(d) == (s and d)
        assert qjit(autograph=True)(lambda d: s or d)(d) == (s or d)

        # Here we perform boolean conversion of a tracer object
        assert qjit(autograph=True)(lambda d: not d)(d) == (not d)
        assert qjit(autograph=True)(lambda: not s)() == (not s)

        # Cases where `d` is 1-st argument are going to fail
        with pytest.raises(TracerBoolConversionError):
            assert qjit(autograph=True)(lambda d: d and s)(d) == (d and s)
        with pytest.raises(TracerBoolConversionError):
            assert qjit(autograph=True)(lambda d: d or s)(d) == (d or s)


@pytest.mark.tf
class TestMixed:
    """Test a mix of supported autograph conversions and Catalyst control flow."""

    def test_force_python_fallbacks(self):
        """Test fallback modes of control-flow primitives."""

        with pytest.warns(UserWarning):

            @qjit(autograph=True)
            def f1():
                acc = 0
                while acc < 5:
                    acc = Failing(acc, "while").val + 1
                    for x in [1, 2, 3]:
                        acc += Failing(x, "for").val
                return acc

            assert f1() == 0 + 1 + sum([1, 2, 3])

    def test_no_python_loops(self):
        """Test AutoGraph behaviour on function with Catalyst loops."""

        @qjit(autograph=True)
        def f():
            @for_loop(0, 3, 1)
            def loop(i, acc):
                return acc + i

            return loop(0)

        assert f() == 3

    def test_cond_if_for_loop_for(self, monkeypatch):
        """Test Python conditionals and loops together with their Catalyst counterparts."""
        monkeypatch.setattr("catalyst.autograph_strict_conversion", True)

        # pylint: disable=cell-var-from-loop

        @qjit(autograph=True)
        def f(x):
            acc = 0
            if x < 3:

                @for_loop(0, 3, 1)
                def loop(_, acc):
                    # Oddly enough, AutoGraph treats 'i' as an iter_arg even though it's not
                    # accessed after the for loop. Maybe because it is captured in the nested
                    # function's closure?
                    # TODO: remove the need for initializing 'i'
                    i = 0
                    for i in range(5):

                        @cond(i % 2 == 0)
                        def even():
                            return i

                        @even.otherwise
                        def even():
                            return 0

                        acc += even()

                    return acc

                acc = loop(acc)

            return acc

        assert f(2) == 18
        assert f(3) == 0

    def test_cond_or(self, monkeypatch):
        """Test Python conditionals in conjunction with and-or statements"""

        monkeypatch.setattr("catalyst.autograph_strict_conversion", True)

        @qjit(autograph=True)
        def f(x):
            if x <= 0.0 or x >= 1.0:
                y = 1
            else:
                y = 0
            return y

        assert f(0) == 1
        assert f(1) == 1
        assert f(0.5) == 0

    def test_while_and(self, monkeypatch):
        """Test Python while-loops in conjunction with and-or statements"""

        monkeypatch.setattr("catalyst.autograph_strict_conversion", True)

        @qjit(autograph=True)
        def f(param):
            n = 0
            while param < 0.5 and n < 3:
                param *= 1.5
                n += 1
            return n

        assert f(0.4) == 1
        assert f(0.1) == 3


if __name__ == "__main__":
    pytest.main(["-x", __file__])
