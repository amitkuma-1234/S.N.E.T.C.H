import os
import torch

model_paths = [
    os.path.join("models", "face_model.pth"),
    os.path.join("models", "best_model.pth"),
]

for path in model_paths:
    if os.path.exists(path):
        try:
            model = torch.load(path, map_location='cpu')
            print(f"Loaded: {path}")
            print("Type:", type(model))
            if isinstance(model, dict):
                print("Keys:", list(model.keys())[:5])
            print("Success loading model")
            break
        except Exception as e:
            print(f"Error loading {path}: {e}")
    else:
        print(f"Missing: {path}")
else:
    print("No emotion model found in models/ folder")
