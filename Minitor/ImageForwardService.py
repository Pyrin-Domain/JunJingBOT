"""
图片批量搬运任务管理（image_forward_service）

`img_fd -g/-u/-a` 创建任务 → `-b` 标记起始、`-e` 标记结束 →
收齐两端后调用 ForwardService.image_forward_batch 把区间内的图片合并转发出去。

任务本身是异步的（等用户分两条消息把 begin / end 补齐），所以单开一个服务存
任务状态，BOTMain 不持有这些中间状态。
"""
import asyncio


class image_forward_service:
    """按群维护「图片批量搬运」任务的起止锚点，收齐后交给 ForwardService 执行。"""

    def __init__(self, forward_service: "ForwardService"):
        self.forward = forward_service
        self.task_dict = {}
        self.lock = asyncio.Lock()

    async def create_task(self, group_id, target_id_list, msg_type):
        # 加锁，确定不会创建多个任务
        group_id = str(group_id)
        async with self.lock:
            if self.task_dict.get(group_id):
                print("Tasl has been created")
                return False
            self.task_dict[group_id] = {
                "begin_id": None,
                "end_id": None,
                "begin_event": asyncio.Event(),
                "end_event": asyncio.Event(),
            }
        asyncio.create_task(self._task_init(group_id, target_id_list, msg_type))
        return True

    async def _task_init(self, group_id, target_id_list, msg_type):
        group_id = str(group_id)
        data = self.task_dict.get(group_id)
        if not data:
            print("理论上不存在该问题")
            return
        await data["begin_event"].wait()
        await data["end_event"].wait()
        await self.forward.image_forward_batch(
            group_id, target_id_list, data["begin_id"], data["end_id"], msg_type
        )
        async with self.lock:
            self.task_dict.pop(group_id)
        return

    async def set_begin_id(self, group_id, begin_id):
        if not begin_id:
            return
        async with self.lock:
            group_id = str(group_id)
            data = self.task_dict.get(group_id)
            if not data:
                print("任务未创建！")
                return
            data["begin_id"] = begin_id
            data["begin_event"].set()
        return

    async def set_end_id(self, group_id, end_id):
        if not end_id:
            return
        async with self.lock:
            group_id = str(group_id)
            data = self.task_dict.get(group_id)
            if not data:
                print("任务未创建！")
                return
            data["end_id"] = end_id
            data["end_event"].set()
        return
