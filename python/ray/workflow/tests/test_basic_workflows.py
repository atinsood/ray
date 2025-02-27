import os
import time

from ray.tests.conftest import *  # noqa

import pytest
import ray
from ray import workflow


def test_basic_workflows(workflow_start_regular_shared):
    @ray.remote
    def source1():
        return "[source1]"

    @ray.remote
    def append1(x):
        return x + "[append1]"

    @ray.remote
    def append2(x):
        return x + "[append2]"

    @ray.remote
    def simple_sequential():
        x = source1.bind()
        y = append1.bind(x)
        return workflow.continuation(append2.bind(y))

    @ray.remote
    def identity(x):
        return x

    @ray.remote
    def simple_sequential_with_input(x):
        y = append1.bind(x)
        return workflow.continuation(append2.bind(y))

    @ray.remote
    def loop_sequential(n):
        x = source1.bind()
        for _ in range(n):
            x = append1.bind(x)
        return workflow.continuation(append2.bind(x))

    @ray.remote
    def nested_step(x):
        return workflow.continuation(append2.bind(append1.bind(x + "~[nested]~")))

    @ray.remote
    def nested(x):
        return workflow.continuation(nested_step.bind(x))

    @ray.remote
    def join(x, y):
        return f"join({x}, {y})"

    @ray.remote
    def fork_join():
        x = source1.bind()
        y = append1.bind(x)
        y = identity.bind(y)
        z = append2.bind(x)
        return workflow.continuation(join.bind(y, z))

    @ray.remote
    def mul(a, b):
        return a * b

    @ray.remote
    def factorial(n):
        if n == 1:
            return 1
        else:
            return workflow.continuation(mul.bind(n, factorial.bind(n - 1)))

    # This test also shows different "style" of running workflows.
    assert workflow.run(simple_sequential.bind()) == "[source1][append1][append2]"

    wf = simple_sequential_with_input.bind("start:")
    assert workflow.run(wf) == "start:[append1][append2]"

    wf = loop_sequential.bind(3)
    assert workflow.run(wf) == "[source1]" + "[append1]" * 3 + "[append2]"

    wf = nested.bind("nested:")
    assert workflow.run(wf) == "nested:~[nested]~[append1][append2]"

    wf = fork_join.bind()
    assert workflow.run(wf) == "join([source1][append1], [source1][append2])"

    assert workflow.run(factorial.bind(10)) == 3628800


def test_async_execution(workflow_start_regular_shared):
    @ray.remote
    def blocking():
        time.sleep(10)
        return 314

    start = time.time()
    output = workflow.run_async(blocking.bind())
    duration = time.time() - start
    assert duration < 5  # workflow.run is not blocked
    assert ray.get(output) == 314


@pytest.mark.skip(reason="Ray DAG does not support partial")
def test_partial(workflow_start_regular_shared):
    ys = [1, 2, 3]

    def add(x, y):
        return x + y

    from functools import partial

    f1 = workflow.step(partial(add, 10)).step(10)

    assert "__anonymous_func__" in f1._name
    assert f1.run() == 20

    fs = [partial(add, y=y) for y in ys]

    @ray.remote
    def chain_func(*args, **kw_argv):
        # Get the first function as a start
        wf_step = workflow.step(fs[0]).step(*args, **kw_argv)
        for i in range(1, len(fs)):
            # Convert each function inside steps into workflow step
            # function and then use the previous output as the input
            # for them.
            wf_step = workflow.step(fs[i]).step(wf_step)
        return wf_step

    assert workflow.run(chain_func.bind(1)) == 7


def test_run_or_resume_during_running(workflow_start_regular_shared):
    @ray.remote
    def source1():
        return "[source1]"

    @ray.remote
    def append1(x):
        return x + "[append1]"

    @ray.remote
    def append2(x):
        return x + "[append2]"

    @ray.remote
    def simple_sequential():
        x = source1.bind()
        y = append1.bind(x)
        return workflow.continuation(append2.bind(y))

    output = workflow.run_async(
        simple_sequential.bind(), workflow_id="running_workflow"
    )
    with pytest.raises(RuntimeError):
        workflow.run_async(simple_sequential.bind(), workflow_id="running_workflow")
    with pytest.raises(RuntimeError):
        workflow.resume_async(workflow_id="running_workflow")
    assert ray.get(output) == "[source1][append1][append2]"


def test_dynamic_output(workflow_start_regular_shared):
    @ray.remote
    def exponential_fail(k, n):
        if n > 0:
            if n < 3:
                raise Exception("Failed intentionally")
            return workflow.continuation(
                exponential_fail.options(**workflow.options(name=f"step_{n}")).bind(
                    k * 2, n - 1
                )
            )
        return k

    # When workflow fails, the dynamic output should points to the
    # latest successful step.
    try:
        workflow.run(
            exponential_fail.options(**workflow.options(name="step_0")).bind(3, 10),
            workflow_id="dynamic_output",
        )
    except Exception:
        pass
    from ray.workflow.workflow_storage import get_workflow_storage

    wf_storage = get_workflow_storage(workflow_id="dynamic_output")
    result = wf_storage.inspect_step("step_0")
    assert result.output_step_id == "step_3"


def test_workflow_error_message():
    storage_url = r"c:\ray"
    expected_error_msg = f"Cannot parse URI: '{storage_url}'"
    if os.name == "nt":

        expected_error_msg += (
            " Try using file://{} or file:///{} for Windows file paths.".format(
                storage_url, storage_url
            )
        )
    if ray.is_initialized():
        ray.shutdown()
    with pytest.raises(ValueError) as e:
        ray.init(storage=storage_url)
    assert str(e.value) == expected_error_msg


def test_options_update():
    from ray.workflow.common import WORKFLOW_OPTIONS

    # Options are given in decorator first, then in the first .options()
    # and finally in the second .options()
    @workflow.options(name="old_name", metadata={"k": "v"})
    @ray.remote(num_cpus=2, max_retries=1)
    def f():
        return

    # name is updated from the old name in the decorator to the new name in the first
    # .options(), then preserved in the second options.
    # metadata and ray_options are "updated"
    # max_retries only defined in the decorator and it got preserved all the way
    new_f = f.options(
        num_returns=2,
        **workflow.options(name="new_name", metadata={"extra_k2": "extra_v2"}),
    )
    options = new_f.bind().get_options()
    assert options == {
        "num_cpus": 2,
        "num_returns": 2,
        "max_retries": 1,
        "_metadata": {
            WORKFLOW_OPTIONS: {
                "name": "new_name",
                "metadata": {"extra_k2": "extra_v2"},
            }
        },
    }


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main(["-v", __file__]))
