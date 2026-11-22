"""Run with: python3 test_pacing.py"""
import asyncio
from types import SimpleNamespace

import pacing


async def main():
    t, sleeps, order = [100.0], [], []

    async def sleep(seconds):
        sleeps.append(round(seconds, 3))
        t[0] += seconds

    def message(channel_id, name, fail=False):
        async def edit(**kwargs):
            order.append((name, round(t[0], 3), kwargs))
            await asyncio.sleep(0)          # other tasks get a turn, like during a real request
            t[0] += 0.2                     # the request takes a moment
            if fail:
                raise RuntimeError("boom")
            return name
        return SimpleNamespace(channel=SimpleNamespace(id=channel_id), edit=edit)

    edit = lambda msg, **kw: pacing.paced_edit(msg, gap=3.5, clock=lambda: t[0], sleep=sleep, **kw)
    # a burst of four edits in one channel and one in another, all started at once
    results = await asyncio.gather(edit(message(1, "a"), embeds=[1]), edit(message(1, "b")), edit(message(2, "x")),
                                   edit(message(1, "c", fail=True)), edit(message(1, "d")), return_exceptions=True)
    assert results[:3] == ["a", "b", "x"] and isinstance(results[3], RuntimeError) and results[4] == "d", results
    one = [(n, when) for n, when, _ in order if n in "abcd"]
    assert [n for n, _ in one] == ["a", "b", "c", "d"], one                      # in order, one at a time
    gaps = [round(b[1] - a[1], 3) for a, b in zip(one, one[1:])]
    assert all(g >= 3.7 - 1e-6 for g in gaps), gaps                             # 3.5s after the previous edit FINISHED
    assert order[0][2] == {"embeds": [1]}                                        # arguments are passed through
    assert next(when for n, when, _ in order if n == "x") < one[1][1]            # the other channel did not wait
    t[0] += 10
    before = len(sleeps)
    await edit(message(1, "late"))
    assert len(sleeps) == before                                                 # no wait when the gap already passed
    print("pacing ok", gaps)


asyncio.run(main())
