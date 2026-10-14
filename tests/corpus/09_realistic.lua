-- A slice of a plausible Roblox-ish script: state tables, methods, closures,
-- varargs, string work and a little metatable abuse.
local Player = {}
Player.__index = Player
function Player.new(name, level)
  return setmetatable({name = name, level = level, items = {}, gold = 0}, Player)
end
function Player:addItem(id, qty, ...)
  qty = qty or 1
  local extra = select("#", ...)
  self.items[id] = (self.items[id] or 0) + qty + extra
  return self.items[id]
end
function Player:describe()
  local names = {}
  for id, n in pairs(self.items) do table.insert(names, id .. "x" .. n) end
  table.sort(names)
  return ("%s (lvl %d) gold=%d items=%s"):format(
    self.name, self.level, self.gold, table.concat(names, ","))
end
local p = Player.new("Ada", 7)
local Tag = {}
Tag.__tostring = function(t) return "<" .. t.id .. ">" end
Tag.__eq = function(a, b) return a.id == b.id end
p:addItem("sword", 2)
p:addItem("potion")
p:addItem("gem", 1, "shiny", "raw")
print(p:describe())
p.gold = p.gold + 100
print(Player.describe(p))
local t1, t2 = setmetatable({id = "a"}, Tag), setmetatable({id = "a"}, Tag)
print(tostring(t1), t1 == t2)
local acc = {}
for i = 1, 5 do
  acc[#acc + 1] = function() return i * i end
end
local out = {}
for _, f in ipairs(acc) do out[#out + 1] = f() end
print(table.concat(out, " "))
local words = {}
for w in ("the quick brown fox"):gmatch("%a+") do words[#words + 1] = w:upper() end
print(table.concat(words, "|"))
print(pcall(function() local q = nil return q.field end))
local function fold(t, f, init)
  local a = init
  for _, v in ipairs(t) do a = f(a, v) end
  return a
end
print(fold({1, 2, 3, 4, 5}, function(a, b) return a + b end, 0))
print(fold({1, 2, 3, 4, 5}, function(a, b) return a * b end, 1))
