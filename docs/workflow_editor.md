# Workflow editor

The editor has a palette, scrollable canvas, and resizable Inspector. It supports Checkpoint, Batch, Slurm, Cloud, Docker, LLM, Native, and Web DAGonStar tasks.

Task names are unique and are the identity used by DAGonStar references. The Batch editor uses Ace with Bash syntax highlighting, automatic statement termination on Enter, and a beautifier. While editing a Batch script, click a canvas task to insert `workflow:///task/output` at the Ace cursor.

Links are visual representations of `workflow:///` references. Applying configuration rebuilds links from all references; imported `nexts` and `prevs` are ignored. The canvas automatically saves edits and provides an **Organize canvas** action.
