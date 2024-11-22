import json
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

def send_notification(message):
    notify_url = "https://open.feishu.cn/open-apis/bot/v2/hook/007c39d4-a4be-431b-b5c4-6e37e04ee4c6"  # 替换为实际的机器人URL
    notify_headers = {'Content-Type': 'application/json'}
    notify_data = {
        "msg_type": "text",
        "content": {
            "text": message
        }
    }
    requests.post(notify_url, headers=notify_headers, json=notify_data)

def check_url(image):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
    }
    try:
        response = requests.head(image['url'], headers=headers, timeout=10)
        if response.status_code != 200:
            message = f"{image['id']} is not available"
            send_notification(message)
            return message
        return None
    except requests.RequestException as e:
        message = f"{image['id']} error: {str(e)}"
        send_notification(message)
        return message

# 从vm-iso.json中读取镜像列表
with open('vm-iso.json', 'r') as f:
    imageList = json.load(f)

# 使用线程池并行检查URL
with ThreadPoolExecutor(max_workers=10) as executor:
    # 提交所有任务
    future_to_url = {executor.submit(check_url, image): image for image in imageList}
    
    # 获取结果
    for future in as_completed(future_to_url):
        result = future.result()
        if result:
            print(result)
        