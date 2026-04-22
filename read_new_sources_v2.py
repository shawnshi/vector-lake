import os

files = [
    'MEMORY/raw/article/医疗数字化三十讲/第二十一讲：人工智能（AI）与机器学习（ML）：医疗“最强大脑”的崛起.md',
    'MEMORY/raw/article/医疗数字化三十讲/第二十七讲：项目管理与交付：确保蓝图不只是“墙上的画”.md',
    'MEMORY/raw/article/医疗数字化三十讲/第二十三讲：区块链与数据安全：构建医疗信任体系的基石.md',
    'MEMORY/raw/article/医疗数字化三十讲/第二十九讲：沟通与引导：与院长、主任、工程师的“同频对话”.md',
    'MEMORY/raw/article/医疗数字化三十讲/第二十二讲：生成式 AI 与大语言模型（LLM）：医疗行业的“Copilot”.md'
]
root = '../../'

for f in files:
    path = os.path.join(root, f)
    print(f'=== FILE: {f} ===')
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as s:
                print(s.read())
        else:
            print(f'File not found at {path}')
    except Exception as e:
        print(f'Error reading {path}: {e}')
    print('=== END ===')
