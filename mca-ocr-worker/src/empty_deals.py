import os
import shutil

def empty_directory(dir_path: str) -> None:
    if not os.path.isdir(dir_path):
        raise ValueError(f"{dir_path} is not a valid directory")

    for entry in os.listdir(dir_path):
        full_path = os.path.join(dir_path, entry)

        if os.path.isfile(full_path) or os.path.islink(full_path):
            os.remove(full_path)
        elif os.path.isdir(full_path):
            shutil.rmtree(full_path)

if __name__ == "__main__":
    # Change this to your actual deals directory
    deals_dir = r"/opt/apps/Nick_CasaCapital_LLM/casa-capital/deals"
    # For Windows example:
    # deals_dir = r"D:\Project\Nick_CasaCapital_LLM\casa-capital\deals"

    empty_directory(deals_dir)
    print(f"Emptied directory: {deals_dir}")

# python src/empty_deals.py