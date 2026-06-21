# Task Manager & Queue Semantics

Since document conversions involve CPU-intensive processes, they cannot run within the FastAPI main event loop. Marker UI uses a custom asynchronous queue manager to execute jobs sequentially in the background.

---

## Process Flow

1. **Queueing**: When a user uploads a document, the route creates a database record with `status="pending"` and places the job metadata into the `TaskManager` in-memory queue.
2. **Executor Backends**:
   - The task manager routes jobs to one of two execution backends:
     - **ThreadExecutorBackend**: Runs conversions in a `ThreadPoolExecutor` inside the main process. This is the default for CPU-only and single-GPU environments.
     - **ProcessExecutorBackend**: Spawns one worker process per GPU (pinned to `cuda:i` using `multiprocessing.Pool`) to scale conversions in parallel.
3. **Log & Progress Interception**:
   - **Thread Backend**: Captures logs using a custom thread-local logging handler (`JobLogHandler`) and taps into `tqdm` progress bars.
   - **Process Backend**: Workers stream progress, console logs, and final results back to the parent process using IPC queues (`WorkerEvent` objects).
   - Captured logs are appended to the database and streamed to active clients via Server-Sent Events (SSE).
4. **Completion**:
   - Upon successful completion, the Task Manager saves the result paths and updates the database record to `completed`.
   - If an exception occurs or a worker crashes, the job status is set to `failed` and the traceback is captured.
