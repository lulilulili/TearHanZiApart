# -*- coding: utf-8 -*-
"""strokelab — 汉字矢量笔画拆解（纯矢量，无像素/SDF 判定）。

用法：
    from strokelab import DataHub, FontEntry, runPipeline
    hub = DataHub(root)           # root 含 makemeahanzi-master / hanzi_chaizi-master
    font = FontEntry("x.ttf"); font.buildLibraryB(hub)
    result = runPipeline(hub, font, "永")
"""

from .datahub import DataHub, DEFAULT_CHIPS
from .fonthub import FontEntry
from .pipeline import runPipeline

__version__ = "0.1.0"
__all__ = ["DataHub", "FontEntry", "runPipeline", "DEFAULT_CHIPS"]
