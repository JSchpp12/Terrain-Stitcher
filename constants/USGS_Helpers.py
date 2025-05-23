#will contain known information regarding usgs storage information

import os

class Project: 
    def __init__(self, project_id : int, project_files : list):
        self.project_id = project_id
        self.project_files = project_files

class USGS_Known_Projects:
    registered_project_id_to_file = {
        "5e83a2397d63a400" : "201303_vinton_county_oh_1ft_sp_cnir_images.txt"
    }

    def find_file(directory : str, filename : str) -> str:
        """
        Searches for the specified file within the given directory and its subdirectories.

        Parameters:
            directory (str): The directory to search.
            filename (str): The name of the file to find.

        Returns:
            str: The full path to the file if found, otherwise None.
        """

        for root, dirs, files in os.walk(directory):
            if filename in files:
                return os.path.join(root, filename)
            else:
                for fDir in dirs:
                    deep_search = USGS_Known_Projects.find_file(os.path.join(root, fDir), filename)
                    if deep_search is not None:
                        return deep_search

        return None
    
    def parse_data_file(file): 
        if not os.path.isfile(file):
            raise FileNotFoundError(f"File not found: {file}")
        
        files = []
        with open(file, 'r') as f:
            lines = f.readlines()
            for line in lines:
                #each line contains the parent directory in the line. Dont need this

                cleaned = line.split('/')[-1].strip()
                files.append(cleaned)

        return files

    def __init__(self): 
        projects_dir = os.path.dirname(os.path.abspath(__file__))
        self.project_files = {}

        for project_id  in self.registered_project_id_to_file:
            project_file_name = self.registered_project_id_to_file[project_id]
            file = USGS_Known_Projects.find_file(projects_dir, project_file_name)
            if file is None:
                raise Exception("Failed to find datafile")
            
            fileNames = USGS_Known_Projects.parse_data_file(file)
            
            if project_id not in self.project_files:
                self.project_files[project_id] = fileNames
            else:
                raise Exception("Duplicate project")

    def getProjectID(self, chunkFileName): 
        for project_id in self.project_files:
            if chunkFileName in self.project_files[project_id]:
                return project_id
        
        return None

if __name__ == "__main__": 
    usgs_knowns = USGS_Known_Projects()