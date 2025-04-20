import queue
import threading
import enum
import abc
import subprocess
import os
import json
import hashlib
from concurrent.futures import ThreadPoolExecutor
from typing import Set, Callable, Any

class Status(enum.Enum):
    UNCHANGED = "unchanged"
    CREATED = "created"
    DELETED = "deleted"
    MODIFIED = "modified"

class BuildFailure(Exception):
    """Raised when an action fails, canceling the build."""
    pass

class Action(abc.ABC):
    """Base class for actions executed by Command nodes."""
    @abc.abstractmethod
    def execute(self, node: 'Command', predecessors: Set['Node'], successors: Set['Node']) -> bool:
        """Execute the action for the given node, returning True for success, False for failure."""
        pass

class CommandLineAction(Action):
    """Action that executes a command-line string."""
    def __init__(self, command: str):
        self.command = command
    
    def execute(self, node: 'Command', predecessors: Set['Node'], successors: Set['Node']) -> bool:
        try:
            subprocess.run(self.command, shell=True, check=True)
            print(f"{self.command}")
            return True
        except subprocess.CalledProcessError as e:
            print(f"Error: {e}")
            return False
    
    def __repr__(self):
        return f"CommandLineAction(command={self.command!r})"

class MakeDirAction(Action):
    """Action that creates a directory."""
    def __init__(self, file_loader: 'FileLoader' = None):
        self.file_loader = file_loader
    
    def execute(self, node: 'Command', predecessors: Set['Node'], successors: Set['Node']) -> bool:
        # Find directory in successors
        dir_file = None
        for succ in successors:
            if isinstance(succ, File):
                dir_file = succ
                break
        if not dir_file:
            print(f"Error: No directory file found in successors of {node}")
            return False
        
        # Create the directory
        try:
            os.makedirs(dir_file.path, exist_ok=True)
            print(f"mkdir {dir_file.path}")
            if self.file_loader and dir_file:
                dir_file.hash = ""
                cached_hash = self.file_loader._cache.get(os.path.relpath(dir_file.path, self.file_loader.root_dir), "")
                dir_file.status = Status.CREATED if cached_hash == "" else Status.UNCHANGED
            return True
        except OSError as e:
            print(f"Error creating directory {dir_file.path}: {e}")
            return False
    
    def __repr__(self):
        return f"MakeDir(file_loader={self.file_loader!r})"

class CompileCppAction(Action):
    """Action that compiles a .cpp file to a .o file using g++."""
    def __init__(self, file_loader: 'FileLoader' = None):
        self.file_loader = file_loader
    
    def execute(self, node: 'Command', predecessors: Set['Node'], successors: Set['Node']) -> bool:
        cpp_file = None
        for pred in predecessors:
            if isinstance(pred, File) and pred.path.endswith('.cpp'):
                cpp_file = pred
                break
        if not cpp_file:
            print(f"Error: No .cpp file found in predecessors of {node}")
            return False
        
        o_file = None
        for succ in successors:
            if isinstance(succ, File) and succ.path.endswith('.o'):
                o_file = succ
                break
        if not o_file:
            print(f"Error: No .o file found in successors of {node}")
            return False
        
        cpp_rel_path = os.path.relpath(cpp_file.path, self.file_loader.root_dir)
        o_rel_path = os.path.relpath(o_file.path, self.file_loader.root_dir)
        command = f"g++ -c {cpp_rel_path} -o {o_rel_path} -fPIC"
        try:
            subprocess.run(command, shell=True, check=True, cwd=self.file_loader.root_dir)
            print(f"{command}")
            if self.file_loader and o_file:
                o_file.hash = self.file_loader._compute_hash(o_file.path)
                cached_hash = self.file_loader._cache.get(os.path.relpath(o_file.path, self.file_loader.root_dir), "")
                o_file.status = Status.CREATED if cached_hash == "" else Status.MODIFIED if o_file.hash != cached_hash else Status.UNCHANGED
            return True
        except subprocess.CalledProcessError as e:
            print(f"Error: {e}")
            return False    

    def __repr__(self):
        return f"CompileCppAction(file_loader={self.file_loader!r})"

class LinkCppSharedAction(Action):
    """Action that links .o files into a .so shared library using g++."""
    def __init__(self, file_loader: 'FileLoader' = None):
        self.file_loader = file_loader

    def execute(self, node: 'Command', predecessors: Set['Node'], successors: Set['Node']) -> bool:
        o_files = [pred for pred in predecessors if isinstance(pred, File) and pred.path.endswith('.o')]
        if not o_files:
            print(f"Error: No .o files found in predecessors of {node}")
            return False
        
        so_file = None
        for succ in successors:
            if isinstance(succ, File) and succ.path.endswith('.so'):
                so_file = succ
                break
        if not so_file:
            print(f"Error: No .so file found in successors of {node}")
            return False
        
        o_rel_paths = ' '.join(os.path.relpath(o.path, self.file_loader.root_dir) for o in o_files)
        so_rel_path = os.path.relpath(so_file.path, self.file_loader.root_dir)
        command = f"g++ -shared {o_rel_paths} -o {so_rel_path}"
        try:
            subprocess.run(command, shell=True, check=True, cwd=self.file_loader.root_dir)
            print(f"{command}")
            if self.file_loader and so_file:
                so_file.hash = self.file_loader._compute_hash(so_file.path)
                cached_hash = self.file_loader._cache.get(os.path.relpath(so_file.path, self.file_loader.root_dir), "")
                so_file.status = Status.CREATED if cached_hash == "" else Status.MODIFIED if so_file.hash != cached_hash else Status.UNCHANGED
            return True
        except subprocess.CalledProcessError as e:
            print(f"Error: {e}")
            return False
    
    def __repr__(self):
        return f"LinkCppSharedAction(file_loader={self.file_loader!r})"

class LinkCppAppAction(Action):
    """Action that links .o files and .so libraries into an executable using g++."""
    def __init__(self, file_loader: 'FileLoader' = None):
        self.file_loader = file_loader

    def execute(self, node: 'Command', predecessors: Set['Node'], successors: Set['Node']) -> bool:
        o_files = [pred for pred in predecessors if isinstance(pred, File) and pred.path.endswith('.o')]
        so_files = [pred for pred in predecessors if isinstance(pred, File) and pred.path.endswith('.so')]
        if not o_files:
            print(f"Error: No .o files found in predecessors of {node}")
            return False
        
        exe_file = None
        for succ in successors:
            if isinstance(succ, File) and not succ.path.endswith(('.o', '.so', '.cpp', '.h')):
                exe_file = succ
                break
        if not exe_file:
            print(f"Error: No executable file found in successors of {node}")
            return False
        
        lib_dirs = set(os.path.dirname(so.path) for so in so_files)
        lib_names = [os.path.basename(so.path).replace('lib', '').replace('.so', '') for so in so_files]
        lib_flags = ' '.join(f"-L {os.path.relpath(dir, self.file_loader.root_dir)} -l{name}" for dir, name in zip(lib_dirs, lib_names)) if so_files else ''
        
        o_rel_paths = ' '.join(os.path.relpath(o.path, self.file_loader.root_dir) for o in o_files)
        exe_rel_path = os.path.relpath(exe_file.path, self.file_loader.root_dir)
        command = f"g++ {o_rel_paths} -o {exe_rel_path} {lib_flags}".strip()
        try:
            subprocess.run(command, shell=True, check=True, cwd=self.file_loader.root_dir)
            print(f"{command}")
            if self.file_loader and exe_file:
                exe_file.hash = self.file_loader._compute_hash(exe_file.path)
                cached_hash = self.file_loader._cache.get(os.path.relpath(exe_file.path, self.file_loader.root_dir), "")
                exe_file.status = Status.CREATED if cached_hash == "" else Status.MODIFIED if exe_file.hash != cached_hash else Status.UNCHANGED
            return True
        except subprocess.CalledProcessError as e:
            print(f"Error: {e}")
            return False

    def __repr__(self):
        return f"LinkCppAppAction(file_loader={self.file_loader!r})"

class FileLoader:
    def __init__(self, cache_file: str, root_dir: str):
        self.cache_file = cache_file
        self.root_dir = root_dir
        self._cache = self._load_cache()
    
    def _load_cache(self) -> dict:
        try:
            with open(self.cache_file, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
    
    def _compute_hash(self, path: str) -> str:
        if not os.path.exists(path) or os.path.isdir(path):
            return ""
        with open(path, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()
    
    def load_file(self, path: str) -> 'File':
        abs_path = os.path.join(self.root_dir, path) if not os.path.isabs(path) else path
        rel_path = os.path.relpath(abs_path, self.root_dir)
        current_hash = self._compute_hash(abs_path)
        cached_hash = self._cache.get(rel_path, "")
        
        if not os.path.exists(abs_path):
            status = Status.DELETED
        elif cached_hash == "" and current_hash != "":
            status = Status.CREATED
        elif current_hash == cached_hash and current_hash != "":
            status = Status.UNCHANGED
        else:
            status = Status.MODIFIED
        
        file_node = File(abs_path, timestamp=os.path.getmtime(abs_path) if os.path.exists(abs_path) else 0.0, hash=current_hash, status=status)
        self._cache[rel_path] = current_hash  # Update cache immediately
        return file_node
    
    def save_cache(self, files: Set['File'] = None):
        cache = self._cache if files is None else {}
        if files:
            for file in files:
                rel_path = os.path.relpath(file.path, self.root_dir)
                cache[rel_path] = file.hash or ""
        os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
        with open(self.cache_file, 'w') as f:
            json.dump(cache, f, indent=2)

class ExecuteOnTouchedAction(Action):
    """Action that executes a wrapped action only if any predecessor has a touched status."""
    def __init__(self, action: Action, file_loader: 'FileLoader'):
        self.action = action
        self.file_loader = file_loader

    def execute(self, node: 'Command', predecessors: Set['Node'], successors: Set['Node']) -> bool:
        should_execute = any(
            isinstance(pred, File) and pred.status in (Status.CREATED, Status.MODIFIED, Status.DELETED)
            for pred in predecessors
        )
        
        files = {pred for pred in predecessors if isinstance(pred, File)} | \
                {succ for succ in successors if isinstance(succ, File)}
        
        if should_execute:
            success = self.action.execute(node, predecessors, successors)
            if success:
                for file in files:
                    if os.path.exists(file.path):
                        file.hash = self.file_loader._compute_hash(file.path)
                        cached_hash = self.file_loader._cache.get(os.path.relpath(file.path, self.file_loader.root_dir), "")
                        file.status = Status.CREATED if cached_hash == "" else Status.MODIFIED if file.hash != cached_hash else Status.UNCHANGED
                    else:
                        file.hash = ""
                        file.status = Status.DELETED
                self.file_loader.save_cache(files)
            return success
        else:
            for file in files:
                if os.path.exists(file.path):
                    file.hash = self.file_loader._compute_hash(file.path)
                    cached_hash = self.file_loader._cache.get(os.path.relpath(file.path, self.file_loader.root_dir), "")
                    file.status = Status.UNCHANGED if file.hash == cached_hash and file.hash != "" else Status.MODIFIED
                else:
                    file.hash = ""
                    file.status = Status.DELETED
            self.file_loader.save_cache(files)
            return True

    def __repr__(self):
        return f"ExecuteOnTouchedAction(action={self.action!r}, file_loader={self.file_loader!r})"

class Node:
    """Base class for nodes in the dependency graph."""
    def __init__(self):
        self.predecessors: Set['Node'] = set()
        self.successors: Set['Node'] = set()
    
    def add_predecessors(self, *predecessors):
        """Add predecessors to this node and set this node as their successor."""
        for pred in predecessors:
            self.predecessors.add(pred)
            pred.successors.add(self)
    
    def __repr__(self):
        return f"Node(predecessors={set(id(p) for p in self.predecessors)}, successors={set(id(s) for s in self.successors)})"

class Data(Node):
    """Node representing data entities like files."""
    def __init__(self, status: Status = Status.UNCHANGED):
        super().__init__()
        self.status = status
    
    def __repr__(self):
        return f"Data(status={self.status})"

class File(Data):
    """Node representing a file with path, timestamp, and hash."""
    def __init__(self, path: str, timestamp: float = 0.0, hash: str = "", status: Status = Status.UNCHANGED):
        super().__init__(status)
        self.path = path
        self.timestamp = timestamp
        self.hash = hash
    
    def __repr__(self):
        return f"File(path={self.path!r}, timestamp={self.timestamp}, hash={self.hash!r}, status={self.status})"

class Command(Node):
    """Node representing actions that read predecessors and write successors."""
    def __init__(self, action: Action = None):
        super().__init__()
        if action:
            self.action = action
    
    def __repr__(self):
        return f"Command(action={self.action!r})"

def execute_dependency_graph(*targets, n_threads: int = 10) -> None:
    """
    Execute the dependency graph for the given targets in topological order, using parallel execution.
    Executes the node's action.execute() if the node has an 'action' attribute that is an Action.
    Cancels the build if any action returns False.
    Enforces alternating Data and Command nodes.
    
    Args:
        *targets: Node objects (Data, File, or Command) with predecessors (set) and successors (set); action (Action) is optional.
        n_threads: Number of threads for parallel execution (default 10).
    
    Raises:
        ValueError: If targets are invalid or empty.
        BuildFailure: If an action fails (returns False).
        RuntimeError: If the graph has cycles, nodes have invalid attributes, or the alternating structure is violated.
    """
    if not targets:
        raise ValueError("At least one target must be provided")

    # Collect all relevant nodes (subgraph reachable from targets) and detect cycles
    nodes = set()
    visiting = set()  # Nodes in the current DFS path
    def collect_nodes(node):
        if node in visiting:
            raise RuntimeError(f"Cycle detected involving {node}")
        if node in nodes:
            return
        visiting.add(node)
        # Validate node attributes
        if not (hasattr(node, 'predecessors') and isinstance(node.predecessors, set) and
                hasattr(node, 'successors') and isinstance(node.successors, set)):
            raise RuntimeError(f"Invalid node {node}: must have predecessors (set) and successors (set)")
        # Validate alternating structure
        if isinstance(node, (Data, File)):
            for pred in node.predecessors:
                if not isinstance(pred, Command):
                    raise RuntimeError(f"Data/File node {node} has invalid predecessor {pred}: must be Command")
            for succ in node.successors:
                if not isinstance(succ, Command):
                    raise RuntimeError(f"Data/File node {node} has invalid successor {succ}: must be Command")
        elif isinstance(node, Command):
            for pred in node.predecessors:
                if not isinstance(pred, (Data, File)):
                    raise RuntimeError(f"Command node {node} has invalid predecessor {pred}: must be Data or File")
            for succ in node.successors:
                if not isinstance(succ, (Data, File)):
                    raise RuntimeError(f"Command node {node} has invalid successor {succ}: must be Data or File")
        else:
            raise RuntimeError(f"Invalid node {node}: must be Data, File, or Command")
        nodes.add(node)
        for pred in node.predecessors:
            collect_nodes(pred)
        visiting.remove(node)

    # Traverse predecessors to collect nodes
    for target in targets:
        collect_nodes(target)

    # Compute in-degrees for topological sorting (number of unexecuted predecessors)
    in_degree = {node: len(node.predecessors & nodes) for node in nodes}
    
    # Initialize ready queue with nodes that have no predecessors
    ready = queue.SimpleQueue()
    for node in nodes:
        if in_degree[node] == 0:
            ready.put(node)

    # Track completed nodes and build status
    completed = set()
    lock = threading.Lock()
    build_failed = False

    def execute_node(node):
        """Execute a node's action and return success status."""
        nonlocal build_failed
        if build_failed:
            return False
        if hasattr(node, 'action') and isinstance(node.action, Action):
            success = node.action.execute(node, node.predecessors, node.successors)
            if not success:
                build_failed = True
                raise BuildFailure(f"Build failed at {node}")
            return success
        return True

    # Execute nodes in parallel
    with ThreadPoolExecutor(max_workers=max(1, n_threads)) as executor:
        futures = []
        node_futures = {}
        while not ready.empty() or futures:
            # Submit tasks for ready nodes if build hasn't failed
            with lock:
                if not build_failed:
                    while not ready.empty() and len(futures) < n_threads:
                        node = ready.get()
                        future = executor.submit(execute_node, node)
                        node_futures[future] = node
                        futures.append(future)
            
            # Wait for at least one task to complete
            if futures:
                from concurrent.futures import wait, FIRST_COMPLETED
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    node = node_futures[future]
                    try:
                        success = future.result()
                        if success:
                            with lock:
                                # Update successors' in-degrees
                                for succ in node.successors & nodes:
                                    in_degree[succ] -= 1
                                    if in_degree[succ] == 0 and not build_failed:
                                        ready.put(succ)
                                completed.add(node)
                    except BuildFailure as e:
                        with lock:
                            build_failed = True
                            print(f"Build canceled: {e}")
                    futures.remove(future)
                    del node_futures[future]

        # Check if all nodes were processed (unless build failed)
        if len(completed) != len(nodes) and not build_failed:
            unprocessed = nodes - completed
            unprocessed_info = "\n".join(
                f"Node {node}: in_degree={in_degree[node]}, predecessors={[p for p in node.predecessors]}"
                for node in unprocessed
            )
            raise RuntimeError(
                f"Execution incomplete: possible graph inconsistency\n"
                f"Unprocessed nodes ({len(unprocessed)}):\n{unprocessed_info}"
            )
