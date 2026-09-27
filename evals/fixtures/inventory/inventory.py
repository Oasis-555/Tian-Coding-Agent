class Inventory:
    def __init__(self):
        self._stock = {}

    def add(self, name, amount):
        self._stock[name] = amount

    def remove(self, name, amount):
        self._stock[name] += amount

    def stock(self, name):
        return self._stock[name]
