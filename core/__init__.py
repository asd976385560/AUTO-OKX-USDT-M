# -*- coding: utf-8 -*-
"""core/ —— V2.0 确定性业务层（可移植·P1 核心层）。

风控闸 / 下单 / 派发 / 简报等确定性业务逻辑收在本包；平台绑定（起 agent / 推送 /
cron 注册）走 collectors/trigger_agent.py 适配层，换平台只换适配层。
本包内模块均跨平台（纯 Python + SQLite + 约定），不依赖 pwsh wrapper。
"""
