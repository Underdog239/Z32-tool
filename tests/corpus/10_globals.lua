-- Globals: plain assignment, function statements, reads, and mixed use.
x = 5
print(x, type(x))
x = x + 1
print(x)
function gmul(a, b) return a * b end
print(gmul(6, 7))
count = 0
function bump()
  count = count + 1
  return count
end
print(bump(), bump(), count)
TABLE_G = {n = 1}
TABLE_G.n = TABLE_G.n + 1
print(TABLE_G.n, type(TABLE_G))
local keep = x
print(keep)
_G.direct = "via _G"
print(direct)
