import json
import requests
import hashlib
imageList = []

# 从vm-iso.json中读取镜像列表
with open('vm-iso.json', 'r') as f:
    imageList = json.load(f)


for image in imageList:
    response = requests.head(image['url'])
    if response.status_code != 200:
        print(f"{image['id']} is not available")
        continue
    
    if image['hash'] != '':
        print(f"{image['id']} hash already exists")
        continue

    # download image to temp file
    temp_file = f"/tmp/{image['id']}.iso"
    with open(temp_file, 'wb') as f:
        f.write(response.content)

    # generate hash
    hash = hashlib.sha256(open(temp_file, 'rb').read()).hexdigest()

