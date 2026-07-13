import sys

def main():
    print("[DEPRECATED] launch_janitor_swarm.py is no longer supported in V11.2 Architecture.")
    print("Do not use subprocess.Popen to launch orphan CLI processes.")
    print("Instead, ask the agent to use `invoke_subagent` to spawn the Janitor Swarm directly from within the sandbox.")
    sys.exit(1)

if __name__ == "__main__":
    main()
