# data.py —— 账号 credential 常量（readme 注明存 SESSDATA / BUVID3 等）
# 这些属于敏感凭证，请勿提交到公开仓库；建议通过环境变量注入。
import os

# 支持从项目根目录的 .env 文件注入环境变量（需安装 python-dotenv）。
# 缺失或加载失败时静默忽略，仍可使用真实环境变量（如 export / 容器注入）。
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

SESSDATA = os.getenv("BILI_SESSDATA", "")
BILI_JCT = os.getenv("BILI_JCT", "")
BUVID3 = os.getenv("BILI_BUVID3", "")

# 说明：main_v2 / send_reply 通过 `from data import SESSDATA, BILI_JCT, BUVID3` 取用。
# 运行前请导出环境变量，例如：
#   export BILI_SESSDATA="xxxx"
#   export BILI_JCT="xxxx"
#   export BILI_BUVID3="xxxx"
