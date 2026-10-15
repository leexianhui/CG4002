using System.Collections.Concurrent;

namespace CG4002.State
{
    /// <summary>
    /// A thread-safe queue of messages. MessageDispatcher has two: Inbound (engine to
    /// visualizer) and Outbound (visualizer to engine). A comms client may enqueue and dequeue
    /// from its network thread; Unity objects are only touched on the main thread.
    /// </summary>
    public class MessageQueue
    {
        readonly ConcurrentQueue<Message> messages = new();

        /// <summary>Safe to call from any thread.</summary>
        public void Enqueue(Message message) => messages.Enqueue(message);

        /// <summary>Safe to call from any thread, but each queue should have exactly one reader.</summary>
        public bool TryDequeue(out Message message) => messages.TryDequeue(out message);

        public int Count => messages.Count;
    }
}
