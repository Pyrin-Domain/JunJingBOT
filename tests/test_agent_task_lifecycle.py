"""Agent 后台任务的引用与取消。"""

import asyncio

from agent.runtime import QQBotAgent


class _Context:
    directed_at_me = True


class _Relations:
    enabled = True

    def should_extract(self, _thread_id, *, direct):
        return direct


def test_relation_tasks_are_retained_and_cancelled_on_close():
    async def exercise():
        agent = QQBotAgent.__new__(QQBotAgent)
        agent.relations = _Relations()
        agent._relation_tasks = set()
        agent._closed = False
        started = asyncio.Event()

        async def wait_forever(*_args):
            started.set()
            await asyncio.Event().wait()

        agent._extract_relations = wait_forever
        task = agent._schedule_relation_extract(_Context(), ["收到"], "group_1")

        assert task is not None
        assert task in agent._relation_tasks
        await started.wait()
        agent.close()
        await asyncio.sleep(0)

        assert task.cancelled()
        assert agent._relation_tasks == set()
        assert agent._schedule_relation_extract(_Context(), ["收到"], "group_1") is None

    asyncio.run(exercise())
