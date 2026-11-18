Java 集合速查（示例笔记）

ArrayList 和 LinkedList 应该怎么选？

ArrayList 随机访问 O(1)、尾部追加均摊 O(1)；LinkedList 头尾增删 O(1)、随机访问 O(n)，实际多以 ArrayList 为主。

ArrayList 的扩容机制是什么？

默认容量 10，扩容为原来的 1.5 倍（oldCapacity + oldCapacity >> 1），用 Arrays.copyOf 复制。

HashSet 是如何判重的？

底层 HashMap，add 时以元素为 key，先比 hashCode 再比 equals，两者都相等才算重复。

为什么 foreach 中删除元素会抛异常？

迭代器 modCount 与集合不一致触发 ConcurrentModificationException，应使用 iterator.remove()。
