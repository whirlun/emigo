1. 右侧栏底部用 Overlay 来划分输出区域和输入区域， 输出区域 read-only, 输入区域支持 @ 文件名补全
2. AI 输出后的语法高亮处理
3. AI 输出完成后， 根据 diff 的内容， 更新 “补丁文件列表”， 并在左边弹出类似 magit 的 diff apply/cancel 操作 ，现在 ediff 的分屏 review diff 太难用了
4. 切换不同的工作区的时候， 侧边栏是否需要同步显示？ 这个我还没有想清楚， 自动切方便， 但是自动切侧边栏的内容是不是也不是用户想要的？
5. 研究 Cursor 的提示词， 看看能否用 RAG 的方式来增强 aider tree-sitter 这种 repomap 的方式？ 我总感觉 Cursor 的那种模式要高级一点， aider 适合自己的项目精确重构， Cursor 适应范围要广很多
