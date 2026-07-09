import os

def get_all_files_in_directory(directory: str) -> list:
    """
    Get all files in a directory with a specific extension.

    Args:
        directory (str): The directory to search in.
        extension (str): The file extension to filter by.

    Returns:
        list: A list of file paths matching the specified extension.
    """
    import os

    if not os.path.isdir(directory):
        raise ValueError(f"The provided path '{directory}' is not a valid directory.")

    return [os.path.join(directory, f) for f in os.listdir(directory) if os.path.isfile(os.path.join(directory, f))]