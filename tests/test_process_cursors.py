import pytest

from webcodex.store import Store


@pytest.fixture
def observations(tmp_path):
    store = Store(tmp_path / "state")
    store.register("local", "instance", {"project": "b" * 64})
    task = store.create_task("owner", "local/project", "Preserve model output")
    sequence = 0

    def add(
        *,
        process_id=None,
        cursor=0,
        output="",
        status="running",
        has_more=False,
        request_id=None,
        truncated=False,
        total_bytes=None,
        task_override=None,
    ):
        nonlocal sequence
        sequence += 1
        selected_task = task_override or task
        tool = "read_process" if process_id else "exec_command"
        args = {"process_id": process_id, "cursor": cursor} if process_id else {"argv": ["fixture"]}
        job_id = store.enqueue(selected_task, tool, args, request_id, 100)["job_id"]
        process_id = process_id or job_id
        store.claim("local", "instance", 100)
        end = cursor + len(output.encode("utf-8"))
        result = {
            "process_id": process_id,
            "status": status,
            "exit_code": None if status == "running" else 0,
            "cursor": cursor,
            "next_cursor": end,
            "output": output,
            "has_more": has_more,
            "truncated": truncated,
            "total_bytes": end if total_bytes is None else total_bytes,
        }
        store.complete("local", job_id, "instance", "succeeded", result)
        return store.job(job_id, "owner")

    yield store, task, add
    store.close()


def test_multiple_ui_reads_leave_every_unread_byte_for_the_model(observations):
    store, task, add = observations
    first = add(output="START\n", request_id="model-command")
    process = first["result"]["process_id"]
    one = add(process_id=process, cursor=6, output="A" * 8192, request_id="card-observe:first")
    last = add(
        process_id=process,
        cursor=8198,
        output="B" * 12,
        status="completed",
        request_id="card-observe:second",
    )
    assert one["output_cursor"] == last["output_cursor"] == 6
    assert last["unread_output"] is True
    assert last["result"]["has_more"] is False
    assert last["result"]["cursor"] == 8198
    assert last["result"]["next_cursor"] == 8210
    assert last["result"]["output"] == "B" * 12
    assert last["next_action"]["arguments"]["cursor"] == 6
    assert last["next_action"]["arguments"]["process_id"] == process
    assert store.process_output_cursors(task["id"], process) == {
        "model_cursor": 6,
        "confirmed_end": 8210,
    }


@pytest.mark.parametrize("prefix", ["card-observe:", "manage-observe:", "watch:"])
def test_every_internal_observer_preserves_the_model_cursor(observations, prefix):
    store, task, add = observations
    first = add(output="start", request_id="model-command")
    process = first["result"]["process_id"]
    observed = add(
        process_id=process, cursor=5, output="finish", status="completed", request_id=prefix + "one"
    )
    assert observed["output_cursor"] == 5
    assert observed["unread_output"]
    assert observed["next_action"]["arguments"]["cursor"] == 5
    assert store.process_output_cursors(task["id"], process)["model_cursor"] == 5


def test_model_reads_advance_its_cursor_and_stop_when_all_output_was_returned(observations):
    store, task, add = observations
    first = add(output="begin", request_id="model-command")
    process = first["result"]["process_id"]
    internal = add(
        process_id=process, cursor=5, output="more-data", status="completed", request_id="watch:one"
    )
    add(process_id=process, cursor=5, output="more-", has_more=True)
    observed = store.job(internal["job_id"], "owner")
    assert observed["output_cursor"] == 10
    assert observed["next_action"]["arguments"]["cursor"] == 10
    add(process_id=process, cursor=10, output="data", status="completed", request_id="model-final")
    observed = store.job(internal["job_id"], "owner")
    assert observed["output_cursor"] == 14
    assert observed["unread_output"] is False
    assert "next_action" not in observed
    assert store.process_output_cursors(task["id"], process)["model_cursor"] == 14


def test_rereading_an_earlier_page_does_not_move_the_model_cursor_backwards(observations):
    store, task, add = observations
    first = add(output="0123456789", status="completed", request_id="model-command")
    process = first["result"]["process_id"]
    add(
        process_id=process,
        cursor=0,
        output="0123",
        status="completed",
        has_more=True,
        request_id="model-reread",
    )
    observed = add(
        process_id=process,
        cursor=4,
        output="456789",
        status="completed",
        request_id="manage-observe:one",
    )
    assert observed["output_cursor"] == 10
    assert observed["unread_output"] is False
    assert "next_action" not in observed


def test_truncated_total_bytes_do_not_create_an_unread_output_loop(observations):
    store, task, add = observations
    first = add(output="START\n", request_id="model-command")
    process = first["result"]["process_id"]
    observed = add(
        process_id=process,
        cursor=6,
        output="x" * 10,
        status="completed",
        request_id="card-observe:truncated",
        truncated=True,
        total_bytes=10_000_000,
    )
    assert observed["unread_output"]
    assert observed["output_cursor"] == 6
    add(
        process_id=process,
        cursor=6,
        output="x" * 10,
        status="completed",
        truncated=True,
        total_bytes=10_000_000,
    )
    reread = store.job(observed["job_id"], "owner")
    assert reread["output_cursor"] == 16
    assert reread["unread_output"] is False
    assert "next_action" not in reread


def test_an_old_internal_has_more_cannot_undo_a_later_model_drain(observations):
    store, task, add = observations
    first = add(output="0123456789", status="completed", request_id="model-command")
    process = first["result"]["process_id"]
    old_page = add(
        process_id=process,
        cursor=0,
        output="0123",
        status="completed",
        has_more=True,
        request_id="card-observe:old-page",
    )
    assert old_page["output_cursor"] == 10
    assert old_page["unread_output"] is False
    assert "next_action" not in old_page


def test_more_output_at_the_model_cursor_keeps_an_action_for_the_same_process(observations):
    store, task, add = observations
    first = add(output="0123456789", request_id="model-command")
    process = first["result"]["process_id"]
    observed = add(
        process_id=process,
        cursor=10,
        output="",
        status="completed",
        has_more=True,
        request_id="watch:pending-output",
    )
    assert observed["output_cursor"] == 10
    assert observed["unread_output"] is True
    assert observed["next_action"]["tool"] == "read_process"
    assert observed["next_action"]["arguments"]["cursor"] == 10


def test_process_cursor_is_scoped_to_the_exact_task_and_process(observations):
    store, task, add = observations
    first = add(output="one", request_id="model-one")
    other = add(output="z" * 100, status="completed", request_id="model-other")
    process = first["result"]["process_id"]
    assert process != other["result"]["process_id"]
    assert store.process_output_cursors(task["id"], process)["model_cursor"] == 3
    assert store.process_output_cursors("not-this-task", process) == {
        "model_cursor": 0,
        "confirmed_end": 0,
    }


def test_bad_legacy_cursor_data_cannot_break_an_internal_observation(observations):
    store, task, add = observations
    first = add(output="one", request_id="model-one")
    process = first["result"]["process_id"]
    with store.db:
        store.db.execute(
            "UPDATE jobs SET result='invalid legacy json' WHERE id=?", (first["job_id"],)
        )
    observed = add(
        process_id=process,
        cursor=3,
        output="two",
        status="completed",
        request_id="card-observe:one",
    )
    assert observed["output_cursor"] == 0
    assert observed["unread_output"] is True
    assert observed["next_action"]["arguments"]["cursor"] == 0
