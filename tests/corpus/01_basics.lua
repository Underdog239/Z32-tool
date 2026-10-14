local a = 1 + 2 * 3
local b = (1 + 2) * 3
local c = 2 ^ 10
local d = 7 % 3
local e = 7 / 2
local f = -a + b
print(a, b, c, d, e, f)
print(a < b, a <= b, a == b, a ~= b, a > b, a >= b)
print(not nil, not false, not 0)
print("concat" .. 1 .. 2 .. "x")
print(#"hello", #"")
local t = {1, 2, 3, nil, 5}
-- `#` on a table with holes may return any border, so assert the property the
-- language actually guarantees and keep the specified parts exact.
print(#t == 3 or #t == 5, t[1], t[5], t[9])
local u = {x = 1, y = 2, [10] = "ten"}
print(u.x, u.y, u[10], u.z)
print(1 and 2, nil and 2, false or 3, 1 or 3)
