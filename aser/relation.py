import hashlib

relation_senses = [
    'Precedence', 'Succession', 'Synchronous',
    'Reason', 'Result',
    'Condition', 'Contrast', 'Concession',
    'Conjunction', 'Instantiation', 'Restatement', 'ChosenAlternative', 'Alternative', 'Exception',
    'Co_Occurrence']

class Relation(object):
    def __init__(self, heid=None, teid=None, relations=None):
        self.heid = heid if heid else ""
        self.teid = teid if teid else ""
        self.rid = Relation.generate_rid(self.heid, self.teid)
        
        self.relations = dict()
        self.update_relations(relations)

    @classmethod
    def generate_rid(cls, heid, teid):
        key = heid + "$" + teid
        return hashlib.sha1(key.encode('utf-8')).hexdigest()

    def update_relations(self, x):
        if x is not None:
            if isinstance(x, dict):
                for r, cnt in x.items():
                    if r not in self.relations:
                        self.relations[r] = cnt
                    else:
                        self.relations[r] += cnt
            elif isinstance(x, (list, tuple)):
                for r in x:
                    if r not in self.relations:
                        self.relations[r] = 1.0
                    else:
                        self.relations[r] += 1.0
            elif isinstance(x, Relation):
                if self.heid == x.heid and self.teid == x.teid:
                    for r, cnt in x.relations.items():
                        if r not in self.relations:
                            self.relations[r] = cnt
                        else:
                            self.relations[r] += cnt

    def to_dict(self):
        return self.__dict__

    def from_dict(self, d):
        for attr_name in self.__dict__:
            self.__setattr__(attr_name, d[attr_name])
        return self