FROM python:3.11-slim

WORKDIR /app

# CPU-only torch/torchvision — keeps the image lean (no CUDA; Docker Desktop has no GPU)
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY rag.py app.py catalog.json ./
COPY .streamlit ./.streamlit

# bge-m3 downloads here on first run; mounted as a volume so it persists across restarts
ENV HF_HOME=/root/.cache/huggingface

EXPOSE 8501
CMD ["streamlit", "run", "app.py"]
