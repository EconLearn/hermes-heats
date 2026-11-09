class LRUCache:
    def __init__(self, capacity):
        self.capacity = capacity
        self.data = {}

    def get(self, key):
        return self.data.get(key, -1)

    def put(self, key, value):
        if len(self.data) >= self.capacity:
            first = next(iter(self.data))
            del self.data[first]
        self.data[key] = value
