# Notes for coding agents

`ssd-refresh` finds large files that read slowly from the D: SSD and rewrites them.
On that drive, data read at full speed when written but drops to 50-100 MB/s after
some weeks. Only a rewrite brings it back.

- Speed tests must bypass the Windows file cache (`FILE_FLAG_NO_BUFFERING`), or they
  measure RAM instead of the drive.
- The copy must write real new data. `shutil.copyfile`, Explorer, `copy` and
  `robocopy` can block-clone on ReFS, which shares the old blocks and rewrites nothing.
- Windows only: the tool calls kernel32 through ctypes.
