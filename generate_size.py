import json
import requests

imageList = []

# 从vm-iso.json中读取镜像列表
with open('vm-iso.json', 'r') as f:
    imageList = json.load(f)


for image in imageList:
    response = requests.head(image['url'])
    if response.status_code != 200:
        print(f"{image['id']} is not available")
        continue
    try:
        size = response.headers['Content-Length']
    except:
        print(f"{image['id']} size not found, url {image['url']}")
        continue
    if image['size'] != int(size):
        print(f"{image['id']} size mismatch: {image['size']} != {size}")
    image['size'] = int(size)

with open('vm-iso.json', 'w') as f:
    # 需要formatrting
    json.dump(imageList, f, indent=4)
