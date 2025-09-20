FROM python:3.11-slim

WORKDIR /app

# Copy requirements and install dependencies
COPY pyproject.toml ./

# Install uv and dependencies
RUN pip install uv
RUN uv venv
RUN uv sync --no-dev

# Copy application code
COPY . .

# Expose port
EXPOSE 8000

# Run the application
CMD [".venv/bin/uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]