"""
跨消息区间任务管理（RangeTaskService）

export / recall / img_fd 的共同点：都要用户分几条消息把 begin_id / end_id
（-b / -e）补齐，两端收齐之后才执行真正的动作。

任务按群存一条，结构固定为：

    {group_id: {"command": "export", "data": {"begin_id": ..., "end_id": ..., ...}}}

- command：这条区间属于哪个命令
- data：两端 id + 该命令自己的业务参数（目标群 / 目标用户 / 图片搬运目标）

所有命令共用同一把锁，命令处理器只负责解析参数，收集状态与完成后的分发
都封装在这里，不再各存一套 tasks + lock。
"""
import asyncio


class RangeTaskService:
    """按群收集跨消息的 begin/end 区间，收齐后回调对应命令执行。"""

    def __init__(self):
        self.task_dict: dict[str, dict] = {}
        self.lock = asyncio.Lock()
        self._handlers: dict[str, object] = {}

    def register(self, command, handler):
        """登记命令完成后的执行函数：handler(event, data)。"""
        self._handlers[str(command)] = handler

    async def submit(
        self,
        command,
        group_id,
        *,
        event=None,
        begin_id=None,
        end_id=None,
        data=None,
        ready_when=None,
    ):
        """登记或更新一条区间任务，收齐后在锁外回调 command 对应的 handler。

        :param command: 命令名（export / recall / img_forward_create_task 等）
        :param group_id: 任务所属群；同一群同时只保留一条未完成任务
        :param event: 触发本次登记的消息事件，原样交给 handler
        :param begin_id: 本次消息给出的起点 message_id
        :param end_id: 本次消息给出的终点 message_id
        :param data: 命令自己的业务参数，非 None 的键合并进 data
        :param ready_when: 额外完成条件（同步函数，入参为 data）；
                           缺省只要求两端都补齐
        :return: 完成态 {"command": ..., "data": ...}；未完成 / 命令冲突返回 None
        """
        if begin_id is None and end_id is None and not data:
            return None
        gid = str(group_id)
        command = str(command)

        completed = None
        async with self.lock:
            task = self.task_dict.get(gid)
            if task is None:
                task = {"command": command, "data": {"begin_id": None, "end_id": None}}
                self.task_dict[gid] = task
            elif task["command"] != command:
                # 同一群只留一条未完成区间，避免不同命令互相覆盖锚点
                print(
                    f"[RangeTask] 群 {gid} 已有 {task['command']} 任务，"
                    f"忽略 {command}"
                )
                return None

            payload = task["data"]
            if data:
                payload.update({key: value for key, value in data.items()
                                if value is not None})
            if begin_id is not None:
                payload["begin_id"] = begin_id
            if end_id is not None:
                payload["end_id"] = end_id

            ready = (
                payload["begin_id"] is not None
                and payload["end_id"] is not None
                and (ready_when is None or bool(ready_when(payload)))
            )
            if not ready:
                return None
            # 先出队再执行，长耗时的搬运 / 撤回不占着锁
            self.task_dict.pop(gid, None)
            completed = {"command": task["command"], "data": dict(payload)}

        handler = self._handlers.get(completed["command"])
        if handler is None:
            print(f"[RangeTask] 命令 {completed['command']} 未登记执行函数")
            return completed
        await handler(event, completed["data"])
        return completed


__all__ = ["RangeTaskService"]
