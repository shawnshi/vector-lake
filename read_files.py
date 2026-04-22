import os
import sys

files = [
    "MEMORY/raw/article/医疗大语言模型应用二十讲/模块四：实践与迭代 —— 驾驭“混沌”与“人性”.md",
    "MEMORY/raw/article/医疗大语言模型应用二十讲/第一讲：重构认知 —— LLM的本质是“概率机器”与“责任黑洞”.md",
    "MEMORY/raw/article/医疗大语言模型应用二十讲/第七讲：主战场（一）—— “根据地”战役 —— 攻克文书，解放医生.md",
    "MEMORY/raw/article/医疗大语言模型应用二十讲/第三讲：风险根源 —— 从技术缺陷到系统性脆弱.md",
    "MEMORY/raw/article/医疗大语言模型应用二十讲/第九讲：主战场（三）—— “人心”战役 —— 赢得患者，锁定未来.md"
]

root = "../../"

for f in files:
    path = os.path.join(root, f)
    print(f"--- START FILE: {f} ---")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as file:
            print(file.read())
    else:
        print(f"ERROR: File {path} not found.")
    print(f"--- END FILE: {f} ---")
