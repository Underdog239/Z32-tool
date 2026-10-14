local acc = 0
for i = 1, 10 do acc = acc + i end
print(acc)
for i = 10, 1, -2 do acc = acc + i end
print(acc)
local n = 0
while n < 5 do n = n + 1 end
print(n)
repeat n = n - 1 until n == 0
print(n)
if n == 0 then print("zero") elseif n == 1 then print("one") else print("many") end
for _, v in ipairs({10, 20, 30}) do acc = acc + v end
print(acc)
for k, v in pairs({a = 1}) do print(k, v) end
do local shadow = 5 print(shadow) end
local i = 0
while true do i = i + 1 if i > 3 then break end end
print(i)
