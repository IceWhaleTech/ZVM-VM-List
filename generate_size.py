import json
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

def check_image_size(image):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
    }
    try:
        response = requests.head(image['url'], headers=headers)
        if response.status_code != 200:
            print(f"{image['id']} is not available")
            return image
        
        size = response.headers.get('Content-Length')
        if not size:
            print(f"{image['id']} size not found, url {image['url']}")
            return image
            
        size = int(size)
        if image['size'] != size:
            print(f"{image['id']} size mismatch: {image['size']} != {size}")
        image['size'] = size
        return image
    except Exception as e:
        print(f"Error processing {image['id']}: {str(e)}")
        return image

# 从vm-iso.json中读取镜像列表
with open('vm-iso.json', 'r') as f:
    imageList = json.load(f)

# 使用线程池并行处理
with ThreadPoolExecutor(max_workers=10) as executor:
    # 提交所有任务
    future_to_image = {executor.submit(check_image_size, image): image for image in imageList}
    
    # 收集结果
    updated_images = []
    for future in as_completed(future_to_image):
        updated_images.append(future.result())

# 写回文件
with open('vm-iso.json', 'w') as f:
    json.dump(updated_images, f, indent=4)
