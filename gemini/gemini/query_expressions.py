'''
Created on Mar 16, 2015

@author: brecht
'''
import abc
from multiprocessing import Pipe
from multiprocessing.process import Process
from cassandra.cluster import Cluster
import array

class Expression(object):
    
    __metaclass__ = abc.ABCMeta

    @abc.abstractmethod
    def evaluate(self, session, starting_set):
        return

    @abc.abstractmethod
    def to_string(self):
        return

    @abc.abstractmethod
    def can_prune(self):
        return True

class Simple_expression(Expression):
    
    def __init__(self, from_table, select_column, where_clause):
        self.table = from_table
        self.select_column = select_column
        self.where_clause = where_clause
  
    def evaluate(self, socket, starting_set):
        
        if len(starting_set) == 0:
            return set()
        
        query = "SELECT %s FROM %s" % (self.select_column, self.table)
        if self.where_clause != "":
            query += " WHERE %s" % self.where_clause            
        if self.can_prune() and not starting_set == "*":
            if self.table == 'samples':
                in_clause = "','".join(starting_set)            
                query += " AND %s IN ('%s')" % (self.select_column, in_clause)
            else:
                in_clause = ",".join(map(lambda x: str(x), starting_set))            
                query += " AND %s IN (%s)" % (self.select_column, in_clause)
        
        return rows_as_set(socket.execute(query))

    def to_string(self):
        return self.where_clause

    def can_prune(self):
        return not any (op in self.where_clause for op in ["<", ">"])
    
class AND_expression(Expression):
    
    def __init__(self, left, right):
        self.left = left
        self.right = right

    def evaluate(self, session, starting_set):
        
        if len(starting_set) == 0:
            return set()
        
        if self.right.can_prune():
            temp = self.left.evaluate(session, starting_set)
            return self.right.evaluate(session, temp)
        elif self.left.can_prune():
            temp = self.right.evaluate(session, starting_set)
            return self.left.evaluate(session, temp)
        else:
            temp = self.left.evaluate(session, starting_set)
            return intersect(temp, self.right.evaluate(session, temp))

    def to_string(self):
        res = "(" + self.left.to_string() + ")" + " AND " + "(" + self.right.to_string() + ")"
        return res

    def can_prune(self):
        return True
    
class OR_expression(Expression):
    
    def __init__(self, left, right):
        self.left = left
        self.right = right
 
    def evaluate(self, session, starting_set):
        
        if len(starting_set) == 0:
            return set()        
        return union(self.left.evaluate(session, starting_set), self.right.evaluate(session, starting_set))

    def to_string(self):
        res = "(" + self.left.to_string() + ")" + " OR " + "(" + self.right.to_string() + ")"
        return res

    def can_prune(self):
        return True
    
class NOT_expression(Expression):
    
    def __init__(self, exp, table, select_column):
        self.exp = exp
        self.table = table
        self.select_column = select_column
 
    def evaluate(self, session, starting_set):
        
        if len(starting_set) == 0:
            return set()        
        elif starting_set == '*':
            correct_starting_set = rows_as_set(session.execute("SELECT %s FROM %s" % (self.select_column, self.table)))
        else:
            correct_starting_set = starting_set
            
        return diff(correct_starting_set, self.exp.evaluate(session, correct_starting_set))

    def to_string(self):
        return "NOT (" + self.exp.to_string() + ")"

    def can_prune(self):
        return True
    
class GT_wildcard_expression(Expression):
    
    def __init__(self, column, wildcard_rule, rule_enforcement, sample_names, db_contact_points, cores_for_eval = 1):
        self.column = column
        self.wildcard_rule = wildcard_rule
        if rule_enforcement.startswith('count'):
            self.rule_enforcement = 'count'
            self.count_comp = rule_enforcement[5:].strip()
        else:
            self.rule_enforcement = rule_enforcement        
        self.names = sample_names
        self.nr_cores = cores_for_eval
        self.db_contact_points = db_contact_points
        
    def to_string(self):
        return "[%s].[%s].[%s].[%s]" % (self.column, "?", self.wildcard_rule, self.rule_enforcement)
    
    def can_prune(self):
        return True
    
    def evaluate(self, session, starting_set):
        
        step = len(self.names) / self.nr_cores
    
        procs = []
        conns = []
        results = []
        
        invert = False
        invert_count = False
        if self.wildcard_rule.startswith('!'):
            corrected_rule = self.wildcard_rule[1:]
            if self.rule_enforcement == 'all':
                target_rule = 'any'
                invert = True
            elif self.rule_enforcement == 'any':
                target_rule = 'all'
                invert = True
            elif self.rule_enforcement == 'none':
                target_rule = 'all'
            elif self.rule_enforcement.startswith('count'):
                target_rule = 'count'
                invert_count = True
        else:
            target_rule = self.rule_enforcement
            corrected_rule = self.wildcard_rule
            
        if starting_set == "*" and (invert or target_rule == 'none'):
            correct_starting_set = array.array('i', rows_as_set(session.execute("SELECT variant_id FROM variants")))
        elif starting_set != "*":
            correct_starting_set = array.array('i', starting_set)
        else:
            correct_starting_set = starting_set
        
        for i in range(self.nr_cores):
            parent_conn, child_conn = Pipe()
            conns.append(parent_conn)
            p = Process(target=eval(target_rule +'_query'), args=(child_conn, self.column, corrected_rule,\
                                                                   correct_starting_set, self.db_contact_points))
            procs.append(p)
            p.start()
            
        for i in range(self.nr_cores):
            n = len(self.names)
            begin = i*step + min(i, n % self.nr_cores) #If act_n % p != 0, first procs get 1 value more, so intervals of subsequent procs shift to the right.
            end = begin + step
            if i < n % self.nr_cores:
                end += 1  
            conns[i].send(self.names[begin:end])                
        
        for i in range(self.nr_cores):
            results.append(conns[i].recv())
            conns[i].close()
        
        for i in range(self.nr_cores):
            procs[i].join()
        
        res = set()    
        
        if target_rule == 'any':
            for r in results:
                res = union(res, r)
        elif target_rule in ['all', 'none']:
            res = results[0]
            for r in results[1:]:
                res = intersect(res, r)
                                
        if invert:
            res = diff(set(correct_starting_set), res)
        
        if target_rule == 'count':
            res_dict = dict()
            for d in results:
                res_dict = add_sub_res_to_count_dict(res_dict, d)            
            if invert_count:
                #TODO: if starting_set == "*", retrieve nr of variants somewhere
                total = len(starting_set)
                for variant, count in res_dict.iteritems():
                    res_dict[variant] = total - count
            res = set([v for v, c in res_dict.iteritems() if eval(str(c) + self.count_comp)])
        
        return res

def diff(list1, list2):
    return list1 - list2
    #return filter(lambda x: not x in list2, list1)

def union(list1, list2):
    return list1 | list2
    #return set(list1).union(list2)

def intersect(list1, list2):
    return list1 & list2
    #return filter(lambda x: x in list2, list1)
    
def rows_as_set(rows):
    s = set()
    for r in rows:
        s.add(r[0])
    return s

def rows_as_list(rows):
    return map(lambda x: x[0], rows)
 
def all_query(conn, field, clause, initial_set, contact_points):
        
    cluster = Cluster(contact_points)
    session = cluster.connect('gemini_keyspace')
    
    names = conn.recv()
    
    results = set(initial_set)
    
    for name in names:
        
        if len(results) == 0:
            break
        
        query = "select variant_id from variants_by_samples_%s WHERE sample_name = '%s' AND %s %s " % (field, name, field, clause)
        
        if not any (op in clause for op in ["<", ">"]):  
            if initial_set != "*":     
                in_clause = ",".join(map(lambda x: str(x), results))
                query += " AND variant_id IN (%s)" % in_clause
            results = rows_as_set(session.execute(query))            
        else:
            row = rows_as_set(session.execute(query))
            if initial_set != "*":
                results = intersect(row, results)
            else:
                results = row
        
    session.shutdown()   
    
    conn.send(results)
    conn.close()

def any_query(conn, field, clause, initial_set, contact_points):
        
    cluster = Cluster(contact_points)
    session = cluster.connect('gemini_keyspace')
    
    names = conn.recv()
    
    results = set()
    
    for name in names:
        
        query = "select variant_id from variants_by_samples_%s WHERE sample_name = '%s' AND %s %s " % (field, name, field, clause)
        
        if initial_set != "*" and not any (op in clause for op in ["<", ">"]):           
            in_clause = ",".join(map(lambda x: str(x), initial_set))
            query += " AND variant_id IN (%s)" % in_clause      
        
        row = rows_as_set(session.execute(query))
        results = union(row, results)
        
    session.shutdown()   
    
    conn.send(results)
    conn.close()

def none_query(conn, field, clause, initial_set, contact_points):
        
    cluster = Cluster(contact_points)
    session = cluster.connect('gemini_keyspace')
    
    names = conn.recv()
    
    results = set(initial_set)
    
    for name in names:
        
        query = "select variant_id from variants_by_samples_%s WHERE sample_name = '%s' AND %s %s " % (field, name, field, clause)
        
        if not any (op in clause for op in ["<", ">"]):           
            in_clause = ",".join(map(lambda x: str(x), results))
            query += " AND variant_id IN (%s)" % in_clause      
        
        row = rows_as_set(session.execute(query))
        results = diff(results, row)
        
    session.shutdown()   
    
    conn.send(results)
    conn.close()   
    
def count_query(conn, field, clause, initial_set, contact_points):
    
    cluster = Cluster(contact_points)
    session = cluster.connect('gemini_keyspace')
    
    names = conn.recv()
    
    results = dict()
    
    for name in names:
        
        query = "select variant_id from variants_by_samples_%s WHERE sample_name = '%s' AND %s %s " % (field, name, field, clause)
        
        if initial_set != "*" and not any (op in clause for op in ["<", ">"]):           
            in_clause = ",".join(map(lambda x: str(x), initial_set))
            query += " AND variant_id IN (%s)" % in_clause      
        
        row = rows_as_set(session.execute(query))
        results = add_row_to_count_dict(results, row)
        
    session.shutdown()   
    
    conn.send(results)
    conn.close()
    
def add_row_to_count_dict(res_dict, variants):
    
    for var in variants:
        if not var in res_dict:
            res_dict[var] = 1
        else:
            res_dict[var] += 1
    
    return res_dict   

def add_sub_res_to_count_dict(res_dict, variants):
    
    for var, count in variants.iteritems():
        if not var in res_dict:
            res_dict[var] = count
        else:
            res_dict[var] += count
    
    return res_dict 
    
