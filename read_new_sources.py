import os

files = [
    'MEMORY/raw/article/医疗大语言模型应用二十讲/第二十讲：终局思考 ——成为“价值枢纽”的操作系统战略.md',
    'MEMORY/raw/article/医疗大语言模型应用二十讲/第二讲：能力真相 —— 作为“智力杠杆”的LLM.md',
    'MEMORY/raw/article/医疗大语言模型应用二十讲/第五讲：生态位博弈 —— 技术选型背后的战略权衡.md',
    'MEMORY/raw/article/医疗大语言模型应用二十讲/第八讲：主战场（二）—— “指挥部”战役 —— 对话院长，创造利润.md',
    'MEMORY/raw/article/医疗大语言模型应用二十讲/第六讲：场景发掘方法论 —— 寻找“痛苦指数”与“支付意愿”的交汇点.md'
]
root = '../../'

for f in files:
    path = os.path.join(root, f)
    print(f'=== FILE: {f} ===')
    try:
        with open(path, 'r', encoding='utf-8') as s:
            print(s.read())
    except Exception as e:
        print(f'Error reading {path}: {e}')
    print('=== END ===')
