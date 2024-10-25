import json
import requests
import hashlib
imageList = []


def loadImageList():
    global imageList
    with open('vm-iso.json', 'r') as f:
        imageList = json.load(f)

def saveImageList():
    global imageList
    with open('vm-iso.json', 'w') as f:
        json.dump(imageList, f, indent=4)

loadImageList()

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
    
    # compute once save once
    image['hash'] = hash
    saveImageList()
