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
        