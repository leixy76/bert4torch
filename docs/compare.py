"""
比较本机和convert_script之间的config是否有差别
"""
import os
import json


def main(local_dir, convert_dir):
    file_list = os.walk(local_dir)
    for dir, sub_dir, files in file_list:
        for file in files:
            if file != 'bert4torch_config.json':
                continue
            local_path = os.path.join(dir, file)
            convert_path = local_path.replace(local_dir, convert_dir)
            if not os.path.exists(convert_path):
                print('[WARNING]', convert_path)
            
            # 加载两边的config
            with open(local_path, 'r', encoding='utf-8') as f:
                local_config = json.load(f)
            with open(convert_path, 'r', encoding='utf-8') as f:
                convert_config = json.load(f)

            break_tag = False
            for local_k, local_v in local_config.items():
                if (local_k not in convert_config) or (local_v != convert_config[local_k]):
                    print(local_path, convert_path, local_k, local_v)
                    break_tag = True
                    break
                else:
                    convert_config.pop(local_k)
            if break_tag:
                break
            else:
                if len(convert_config) != 0:
                    print(local_path, convert_path, convert_config)

if __name__ == '__main__':
    local_dir = 'E:\pretrain_ckpt'
    convert_dir = 'D:\Project\\bert4torch\convert_script'
    main(local_dir, convert_dir)