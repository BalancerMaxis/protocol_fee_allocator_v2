# protocol fee allocator v2
## setup


1. install deps:
   ```
   pip install -r requirements.txt
   ```

2. set up the env vars:
   ```
   cp .env.example .env
   ```
   DRPC_KEY is required, but the  EXPLORER_API_KEY vars are optional, used for deterministic block by timestamp fetching.

## run
```
python main.py
```

## test
   ```
   pip install -r requirements-dev.txt

   pytest -s
   ```