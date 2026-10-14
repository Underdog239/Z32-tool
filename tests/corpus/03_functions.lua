local function add(a, b) return a + b end
print(add(1, 2))
local function multi() return 1, 2, 3 end
print(multi())
print((multi()))
local x, y, z = multi()
print(x, y, z)
local function vararg(...) return select("#", ...), ... end
print(vararg(1, nil, 3))
local function tail(n, acc) if n == 0 then return acc end return tail(n - 1, acc + n) end
print(tail(100, 0))
local function counter()
  local n = 0
  return function() n = n + 1 return n end
end
local c1, c2 = counter(), counter()
print(c1(), c1(), c2())
local t = {v = 42}
function t:get() return self.v end
print(t:get())
local function apply(f, ...) return f(...) end
print(apply(add, 20, 22))
print(type(print), type(1), type("s"), type(nil), type(true), type({}))
