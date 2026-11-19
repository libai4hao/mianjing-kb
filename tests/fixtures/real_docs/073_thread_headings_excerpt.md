#### 9、一个线程如果出现了运行时异常会怎么样

如果这个异常没有被捕获的话，这个线程就停止执行了。另外重要的一点是：如果这个线程持有某个某

**个对象的监视器，那么这个对象监视器会被立即释放**


-----

通过在线程之间共享对象就可以了，然后通过wait/notify/notifyAll、await/signal/signalAll进行唤起和

等待，比方说阻塞队列BlockingQueue就是为线程之间共享数据而设计的


#### 11、sleep方法和wait方法有什么区别

这个问题常问，sleep方法和wait方法都可以用来放弃CPU一定的时间，不同点在于如果线程持有某个


对象的监视器，sleep方法不会放弃这个对象的监视器，wait方法会放弃这个对象的监视器

#### 12、生产者消费者模型的作用是什么


这个问题很理论，但是很重要：

1）通过平衡生产者的生产能力和消费者的消费能力来提升整个系统的运行效率，这是生产者消费者模

型最重要的作用

2）解耦，这是生产者消费者模型附带的作用，解耦意味着生产者和消费者之间的联系少，联系越少越


可以独自发展而不需要收到相互的制约

#### 13、ThreadLocal有什么用

简单说ThreadLocal就是一种以空间换时间的做法，在每个Thread里面维护了一个以开地址法实现的

ThreadLocal.ThreadLocalMap，把数据进行隔离，数据不共享，自然就没有线程安全方面的问题了

#### 14、为什么wait()方法和notify()/notifyAll()方法要在同步块中被调 用

这是JDK强制的，wait()方法和notify()/notifyAll()方法在调用前都必须先获得对象的锁

#### 15、wait()方法和notify()/notifyAll()方法在放弃对象监视器时有什 么区别

wait()方法和notify()/notifyAll()方法在放弃对象监视器的时候的区别在于：wait()方法立即释放对象监

**视器，notify()/notifyAll()方法则会等待线程剩余代码执行完毕才会放弃对象监视器。**

#### 16、为什么要使用线程池

避免频繁地创建和销毁线程，达到线程对象的重用。另外，使用线程池还可以根据项目灵活地控制并发

的数目。

#### 17、怎么检测一个线程是否持有对象监视器


-----

供了一个holdsLock(Object obj)方法，当且仅当对象obj的监视器被某条线程持有的时候才会返回

true，注意这是一个static方法，这意味着"某条线程"指的是当前线程。


#### 18、synchronized和ReentrantLock的区别

synchronized是和if、else、for、while一样的关键字，ReentrantLock是类，这是二者的本质区别。既


然ReentrantLock是类，那么它就提供了比synchronized更多更灵活的特性，可以被继承、可以有方

法、可以有各种各样的类变量，ReentrantLock比synchronized的扩展性体现在几点上：


（1）ReentrantLock可以对获取锁的等待时间进行设置，这样就避免了死锁

（2）ReentrantLock可以获取各种锁的信息


（3）ReentrantLock可以灵活地实现多路通知

另外，二者的锁机制其实也是不一样的。ReentrantLock底层调用的是Unsafe的park方法加锁，


synchronized操作的应该是对象头中mark word，这点我不能确定。

#### 19、ConcurrentHashMap的并发度是什么

ConcurrentHashMap的并发度就是segment的大小，默认为16，这意味着最多同时可以有16条线程操

作ConcurrentHashMap，这也是ConcurrentHashMap对Hashtable的最大优势，任何情况下，

Hashtable能同时有两条线程获取Hashtable中的数据吗？

#### 20、ReadWriteLock是什么

首先明确一下，不是说ReentrantLock不好，只是ReentrantLock某些时候有局限。如果使用

ReentrantLock，可能本身是为了防止线程A在写数据、线程B在读数据造成的数据不一致，但这样，如

果线程C在读数据、线程D也在读数据，读数据是不会改变数据的，没有必要加锁，但是还是加锁了，

降低了程序的性能。

因为这个，才诞生了读写锁ReadWriteLock。ReadWriteLock是一个读写锁接口，
