local t = {}
for i = 1, 20 do t[i] = i * i end
print(#t, t[20])
t.name = "z32"
t["key with space"] = true
print(t.name, t["key with space"])
local nested = {a = {b = {c = 99}}}
print(nested.a.b.c)
local function f() return {1, 2, 3}, "extra" end
local arr, ex = f()
print(#arr, ex)
local big = {}
for i = 1, 50 do big[#big + 1] = i end
print(#big, big[50])
table.sort(big, function(a, b) return a > b end)
print(big[1], big[50])
local s = {}
for i = 1, 5 do s[#s + 1] = tostring(i) end
print(table.concat(s, "-"))
print(table.unpack and "has unpack" or "no unpack")
