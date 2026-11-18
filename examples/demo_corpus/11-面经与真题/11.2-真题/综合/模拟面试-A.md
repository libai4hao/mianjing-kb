模拟面试 A（示例科技 · 后端一面）

HashMap 的底层结构是什么？

JDK 1.8 起是数组 + 链表 + 红黑树：链表长度超过 8 且容量不小于 64 时转红黑树，退化到 6 转回链表。

HashMap 扩容时具体发生了什么？

容量翻倍，元素按 hash 高位重新分布（1.8 用高位运算避免整体 rehash），扩容是 O(n)。

ConcurrentHashMap 如何保证线程安全？

1.8 用 CAS + synchronized 锁单个桶头节点，不再用分段锁；扩容时多线程协助迁移。

为什么重写 equals 必须重写 hashCode？

hashCode 决定落桶位置，只重写 equals 会让逻辑相等的对象落到不同桶，Map 查不到。

ThreadLocal 会内存泄漏吗？

会。ThreadLocalMap 的 key 是弱引用、value 是强引用；线程复用时 value 不被回收，必须 remove。
