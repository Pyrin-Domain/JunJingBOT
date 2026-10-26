import asyncio

from bot.commands import CommandHandlers, _parse_range
from bot.services.range_task import RangeTaskService


def _run(coro):
    return asyncio.run(coro)


def test_submit_collects_anchors_across_messages_and_dispatches():
    async def scenario():
        service = RangeTaskService()
        calls = []

        async def handler(event, data):
            calls.append((event, data))

        service.register("export", handler)
        first_event = {"message_id": 101}
        second_event = {"message_id": 102}

        # 第一条消息只给起点 + 目标，尚未完成
        assert (
            await service.submit(
                "export",
                1001,
                event=first_event,
                begin_id=11,
                data={"target_group": 2002},
            )
            is None
        )
        assert service.task_dict["1001"]["command"] == "export"
        assert service.task_dict["1001"]["data"] == {
            "begin_id": 11,
            "end_id": None,
            "target_group": 2002,
        }

        # 第二条消息补齐终点（group_id 用 str/int 都算同一个群）
        assert (
            await service.submit("export", "1001", event=second_event, end_id=15)
            is not None
        )
        assert service.task_dict == {}
        assert calls == [
            (
                second_event,
                {"begin_id": 11, "end_id": 15, "target_group": 2002},
            )
        ]

    _run(scenario())


def test_ready_when_delays_completion_until_extra_data_arrives():
    async def scenario():
        service = RangeTaskService()
        calls = []

        async def handler(event, data):
            calls.append(data)

        def ready(data):
            return data.get("target_group") is not None

        service.register("export", handler)
        # 两端都齐了，但还没有目标：继续等
        assert (
            await service.submit(
                "export", 7, begin_id=1, end_id=2, ready_when=ready
            )
            is None
        )
        assert "7" in service.task_dict

        assert (
            await service.submit("export", 7, data={"target_group": 99},
                                 ready_when=ready)
            is not None
        )
        assert calls == [{"begin_id": 1, "end_id": 2, "target_group": 99}]
        assert service.task_dict == {}

    _run(scenario())


def test_other_command_cannot_take_over_pending_task():
    async def scenario():
        service = RangeTaskService()

        async def handler(event, data):
            raise AssertionError("不应执行")

        service.register("export", handler)
        service.register("recall", handler)
        assert await service.submit("export", 7, begin_id=1) is None

        assert await service.submit("recall", 7, begin_id=2, end_id=3) is None
        assert service.task_dict["7"]["command"] == "export"
        assert service.task_dict["7"]["data"]["begin_id"] == 1

    _run(scenario())


def test_empty_submit_does_not_create_task():
    async def scenario():
        service = RangeTaskService()
        assert await service.submit("recall", 8) is None
        assert service.task_dict == {}

    _run(scenario())


def test_completed_task_is_removed_even_without_handler():
    async def scenario():
        service = RangeTaskService()
        result = await service.submit("recall", 9, begin_id=1, end_id=2)
        assert result == {
            "command": "recall",
            "data": {"begin_id": 1, "end_id": 2},
        }
        assert service.task_dict == {}

    _run(scenario())


def test_parse_range_begin_only():
    assert _parse_range("recall -b", 111, 222) == (111, None)


def test_parse_range_end_only_uses_reply():
    assert _parse_range("recall -e", 111, 222) == (None, 111)


def test_parse_range_end_only_without_reply_uses_command_message():
    assert _parse_range("recall -end", None, 222) == (None, 222)


def test_parse_range_both_markers():
    # 同一条消息带 -b/-e：起点取回复目标，终点取命令消息本身
    assert _parse_range("img_fd -b -e", 111, 222) == (111, 222)


def test_parse_range_without_markers():
    assert _parse_range("export -g 123", 111, 222) == (None, None)


class _FakeMP:
    def __init__(self):
        self.user_id = 999
        self.history_calls = []
        self.history = []
        self.sent = []

    def get_clean_context(self, event, *args, **kwargs):
        return event["raw_message"]

    def is_reply(self, event):
        return event.get("reply_id")

    async def ensure_connected(self):
        return None

    async def send_message(self, event, message):
        self.sent.append(message)

    async def get_long_history_only_processor(
        self, group_id, start_id, end_id, processor, params
    ):
        self.history_calls.append(
            {
                "group_id": group_id,
                "start_id": start_id,
                "end_id": end_id,
                "processor": processor,
                "params": params,
            }
        )
        for item in self.history:
            await processor(event=item, **params)


class _FakeNC:
    def __init__(self):
        self.deleted = []

    async def get_group_member_info(self, group_id, user_id):
        return {"data": {"role": "owner"}}

    async def delete_msg(self, message_id):
        self.deleted.append(message_id)


class _FakeForward:
    def __init__(self):
        self.batch_calls = []

    async def auto_forward(self, event, **kwargs):
        return False

    async def image_forward_batch(
        self, group_id, target_id_list, begin_id, end_id, msg_type
    ):
        self.batch_calls.append(
            (group_id, target_id_list, begin_id, end_id, msg_type)
        )


def _handlers():
    mp = _FakeMP()
    nc = _FakeNC()
    forward = _FakeForward()
    service = RangeTaskService()
    handlers = CommandHandlers(
        mp=mp, nc=nc, forward=forward, range_tasks=service
    )
    return handlers, service, mp, nc, forward


def test_export_collected_by_shared_range_service():
    async def scenario():
        handlers, service, mp, _nc, forward = _handlers()
        await handlers.export(
            {
                "group_id": 1,
                "message_id": 100,
                "reply_id": 10,
                "raw_message": "export -g 222 -b",
            }
        )
        assert service.task_dict["1"]["command"] == "export"
        assert not mp.history_calls

        await handlers.export(
            {
                "group_id": 1,
                "message_id": 101,
                "reply_id": 20,
                "raw_message": "export -e",
            }
        )
        assert service.task_dict == {}
        assert len(mp.history_calls) == 1
        call = mp.history_calls[0]
        assert (call["start_id"], call["end_id"]) == (10, 20)
        assert call["processor"] == forward.auto_forward
        assert call["params"]["target_groupid"] == [222]

    _run(scenario())


def test_recall_collected_by_shared_range_service():
    async def scenario():
        handlers, service, mp, nc, _forward = _handlers()
        mp.history = [
            {"group_id": 1, "message_id": 11, "user_id": 5},
            {"group_id": 1, "message_id": 12, "user_id": 6},
        ]
        await handlers.recall(
            {
                "message_type": "group",
                "group_id": 1,
                "user_id": 10086,
                "message_id": 300,
                "reply_id": 11,
                "raw_message": "recall -b",
            }
        )
        assert service.task_dict["1"]["command"] == "recall"

        await handlers.recall(
            {
                "message_type": "group",
                "group_id": 1,
                "user_id": 10086,
                "message_id": 301,
                "reply_id": 12,
                "raw_message": "recall -e",
            }
        )
        assert service.task_dict == {}
        assert nc.deleted == [11, 12]
        # Commands._do_recall 里的「[批量撤回] 完成…」发送目前是注释掉的，
        # 所以这里不再断言 mp.sent（改动 Commands.py 后记得同步这条用例）
        assert mp.sent == []

    _run(scenario())


def test_img_fd_collected_by_shared_range_service():
    async def scenario():
        handlers, service, _mp, _nc, forward = _handlers()
        await handlers.img_forward_create_task(
            {"group_id": 1, "message_id": 400, "raw_message": "img_fd -g 333"}
        )
        await handlers.img_forward_create_task(
            {
                "group_id": 1,
                "message_id": 401,
                "reply_id": 30,
                "raw_message": "img_fd -b",
            }
        )
        assert service.task_dict["1"]["command"] == "img_forward_create_task"
        assert not forward.batch_calls

        await handlers.img_forward_create_task(
            {
                "group_id": 1,
                "message_id": 402,
                "reply_id": 40,
                "raw_message": "img_fd -e",
            }
        )
        assert service.task_dict == {}
        assert forward.batch_calls == [(1, [333], 30, 40, "group")]

    _run(scenario())


def test_commands_share_one_lock_and_one_task_dict():
    handlers, service, _mp, _nc, _forward = _handlers()
    assert handlers.range_tasks is service
    assert service.lock is handlers.range_tasks.lock
    assert set(service._handlers) == {
        "export", "recall", "img_forward_create_task"
    }
