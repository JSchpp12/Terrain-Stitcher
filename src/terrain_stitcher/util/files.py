import os

def find_files_with_extension(dir, extension): 
    files = []

    for ele in os.listdir(dir): 
        path = os.path.join(dir, ele)
        
        if os.path.isfile(path): 
            if os.path.splitext(ele)[1] == extension: 
                files.append(path)
        elif os.path.isdir(path): 
            dFiles = find_files_with_extension(path, extension)
            files.extend(dFiles)
        else:
            raise Exception("Unknown error")
        
    return files

def default_compare_fun(element : os.PathLike, file_name_key : str) -> bool: 
    return element == file_name_key

def find_file(directory, file_name, compare_fun = default_compare_fun):
    for ele in os.listdir(directory):
        full_ele = os.path.join(directory, ele)

        if compare_fun(ele, file_name):
            return full_ele
        elif os.path.isdir(full_ele):
            deep_search = find_file(full_ele, file_name, compare_fun)
            if deep_search is not None:
                return deep_search
    return None