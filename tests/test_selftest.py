"""播放链路自检本身的回归。

自检是「播放失败时唯一的确定答案」，所以它自己不能骗人：
  - 没登录 115，要在第 2 步就红，不能等到后面报个莫名其妙的错
  - 映射没命中，要在映射那一步红，并给出可执行的改法
  - 全链路正常时，8 步必须全绿（否则用户会以为还是坏的）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CONFIG_DIR", str(Path(__file__).resolve().parent / "_cfg_selftest"))

from app import config, plex, providers, selftest, u115open  # noqa: E402

BAD: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("  ok   " if ok else "  FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        BAD.append(name)


class FakeResp:
    def __init__(self, payload: dict) -> None:
        self._p = payload

    def json(self) -> dict:
        return self._p


def fake_plex_request(method: str, path: str, **kw):  # noqa: ANN003
    if path.endswith("/all"):
        return FakeResp(
            {
                "MediaContainer": {
                    "Metadata": [
                        {
                            "title": "沙丘2",
                            "Media": [{"Part": [{"file": "/mnt/cd2/115/media/电影/沙丘2.mkv"}]}],
                        }
                    ]
                }
            }
        )
    return FakeResp({})


SECTION = [
    {"key": "1", "title": "电影", "type": "movie", "locations": ["/mnt/cd2/115/media"]},
]


def base_cfg(**over) -> dict:
    raw = {
        "plex": {"url": "http://127.0.0.1:32400", "token": "t"},
        "mounts": [{"plex_prefix": "/mnt/cd2/115/media", "cloud_prefix": "/media"}],
        "provider": {"type": "open115", "force_https": False},
    }
    raw.update(over)
    return config.normalize(raw)


def run(cfg: dict, path: str | None = None) -> selftest.Report:
    with mock.patch.object(config, "load", return_value=cfg), mock.patch.object(
        plex, "sections", return_value=SECTION
    ), mock.patch.object(plex, "request", side_effect=fake_plex_request):
        return selftest.run(path=path, ua="Mozilla/5.0 selftest")


print("=== 1) 没登录 115 官方接口：必须第 2 步就红 ===")
cfg1 = base_cfg()  # 没有 u115_open
rep1 = run(cfg1)
names = [s["step"] for s in rep1.steps]
check("判定为失败", rep1.ok is False)
check("失败点是「115 官方接口已登录」", rep1.failed()[0]["step"] == "115 官方接口已登录", names)
check("给出了重扫码的修法", "扫码" in rep1.failed()[0]["fix"], rep1.failed()[0]["fix"])

print("=== 2) 映射没命中：要在映射那一步红，并说清怎么改 ===")
cfg2 = base_cfg(
    mounts=[{"plex_prefix": "/mnt/cd2", "cloud_prefix": "/"}],
    u115_open={"access_token": "t", "refresh_token": "r", "expires_at": 4102444800, "user_name": "u"},
)
rep2 = run(cfg2)
bad2 = rep2.failed()[0]
# ①填太短时映射会「命中」但云盘路径多一层 /115 —— 这种错最隐蔽，必须红并指出
check("失败点是「映射没有可疑之处」", bad2["step"] == "映射没有可疑之处", [s["step"] for s in rep2.steps])
check("detail 指出多出来的那一层", "115" in (bad2["detail"] or ""), bad2["detail"])
check("提示去用「对照 Plex 实际路径」", "对照" in (bad2["fix"] or ""), bad2["fix"])

print("=== 3) 全链路正常：8 步全绿 ===")
cfg3 = base_cfg(
    u115_open={"access_token": "t", "refresh_token": "r", "expires_at": 4102444800, "user_name": "u"}
)
with mock.patch.object(u115open, "pickcode_by_path", return_value="pcdune"), mock.patch.object(
    providers, "resolve"
) as m_resolve, mock.patch.object(
    selftest, "_probe_url", return_value=(True, "HTTP 206 · video/x-matroska · 收到 1024 字节")
):
    m_resolve.return_value = providers.Link(
        url="https://cdn.115.com/x.mkv?t=1", provider="115open", expires_in=3600
    )
    rep3 = run(cfg3)
check("整体通过", rep3.ok is True, [s["step"] for s in rep3.steps if not s["ok"]])
check("步骤数是 8", len(rep3.steps) == 8, len(rep3.steps))

print("=== 4) 直链拿到了但下载被拒（UA 不匹配）===")
with mock.patch.object(u115open, "pickcode_by_path", return_value="pcdune"), mock.patch.object(
    providers, "resolve"
) as m_resolve, mock.patch.object(
    selftest, "_probe_url", return_value=(False, "HTTP 403（115 拒绝，多半是 UA 不匹配或直链过期）")
):
    m_resolve.return_value = providers.Link(
        url="https://cdn.115.com/x.mkv?t=1", provider="115open", expires_in=3600
    )
    rep4 = run(cfg3)
bad4 = rep4.failed()[0]
check("失败点是「直链真的能下载」", bad4["step"] == "直链真的能下载", [s["step"] for s in rep4.steps])
check("修法提示 UA 模式", "UA" in (bad4["fix"] or ""), bad4["fix"])

print()
if BAD:
    print(f"失败 {len(BAD)} 项：" + "、".join(BAD))
    sys.exit(1)
print("全部通过：自检断在哪一步，就报哪一步。")
