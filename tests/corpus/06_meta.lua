local mt = {}
mt.__index = function(t, k) return "default:" .. tostring(k) end
mt.__add = function(a, b) return "added" end
mt.__call = function(self, x) return x * 2 end
mt.__tostring = function() return "OBJ" end
mt.__len = function() return 42 end
local o = setmetatable({}, mt)
print(o.missing)
print(o + o)
print(o(21))
print(tostring(o))
print(getmetatable(o) == mt)
local counter = setmetatable({}, {__index = {count = 0}})
print(counter.count)
