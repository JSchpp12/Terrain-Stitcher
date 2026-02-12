import os

def find_file(directory, file_name):
    for ele in os.listdir(directory):
        full_ele = os.path.join(directory, ele)

        if file_name == ele:
            return os.path.join(directory, file_name)
        elif os.path.isdir(full_ele):
            deep_search = find_file(full_ele, file_name)
            if deep_search is not None:
                return deep_search
    return None