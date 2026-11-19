思路：利用栈的思想每次遇到 “..” 时，将退栈至上一个 '/' 位置。

1. #include <stdio.h>

2. #include <string.h>

3. #include <stdlib.h>

4.

5. char * convert_path_opt （const char * path ）

6. {

7. char * result = NULL ;

8. int top = 0 ;

9. int path_len = 0 ，

10. index = 0 ，

11. point_cnt = 0 ;

12.

13. / ** <check * /

14. if （NULL == path ）

15. {

16. fprintf （stderr ，“convert_path_opt：invalid argument。\ n”

）;

17. 返回 NULL ;

18. }

19.

20. / ** <分配内存* /

21. path_len = strlen （path ）;

22. result = （char *）malloc （path_len * sizeof （char ））;

23. if （NULL == 结果）

24. {

25. fprintf （stderr ，“convert_path_opt：无法访问 malloc。\

n” ）;

26. 返回 NULL ;

27. }

28.

29. / ** <convert * /

30. while （index < path_len ）

31. {

32. / ** <点之前复制字符。* /

33. while （index < path_len && path [ index ] ！= '。' ）

34. {

35. result [ top ++] = path [ index ++];

36. }

37.

38. / ** <反点。* /

39. for （point_cnt = 0 ;

40. index < path_len && path [ index ] == '。' ;

41. ++ point_cnt ，++ index ）;

42.

43. if （point_cnt == 2 ）

44. {

45. - 顶部;

46. while （- top > 0 && result [ top ] ！= '/' ）;

47. ++ 顶部;

48. }

49.

50. ++ index ;

51. }

52.

53. 结果[ top ] = '\ 0' ;

54.

55. 返回结果;

56.}

57.

58.int main （）

59.{

60. char * path = “/ home / news /。/。/ tmp / game /../” ;

61. char * result = NULL ;

62.

63. result = convert_path_opt （path ）;

64. printf （“\ nResult is％s。\ n” ，result ）;

65.

66. 返回 0 ;

67.}

1.

1. 2. 比较两个字符串的不同，返回不同点的位置（相对第二字符串而言）

1.

2.

1. #include <stdio.h>

3.

4. char * compare_strings （const char * plhs ，

5. const char * prhs ）

6. {

7. int lhs_index = 0 ，

8. rhs_index = 0 ;

9.

10. / ** <检查参数。* /

11. if （NULL == plhs || NULL == prhs ）

12. {

13. fprintf （stderr ，“copmare_strings：invalid argument。\

n” ）;

14. 返回 NULL ;

15. }

16.

17. / ** <比较* /

18. 而（plhs [ lhs_index ] ！= '\ 0'

19. && prhs [ rhs_index ] ！= '\ 0' ）

20. {

21. if （plhs [ lhs_index ] ！= prhs [ rhs_index ] ）

22. 打破;

23. ++ lhs_index ;

24. ++ rhs_index ;

25. }

26.

27. return prhs + rhs_index ;

28.}

29.

30.int main （）

31.{

32. char * plhs = “你好，世界。” ;

33. char * prhs = “你好，世界。” ;

34. char * pdiff = NULL ;

35.

36. pdiff = compare_strings （plhs ，prhs ）;

37. printf （“\ nDifference is％s。\ n” ，pdiff ）;

38.

39. 返回 0 ;

40.}

41.

1.

42.3. 给定一个 100 亿的整数文件，输出前 100 个最小值。

43.

44.

45.

46.　　思路：大数据+ top k 问题。

47.

48.

1. #include <stdio.h>

49.#include <stdlib.h>

50.#include <time.h>

51.

52./ ** <max_heap * /

53.struct max_heap

54.{

55. int current_index ;

56. INT 容量;

57. int * elem ;

58.};

59.typedef struct max_heap max_heap ;

60.

61./ ** <allocate max_heap * /

62.max_heap * alloc_max_heap （const的 INT 帽）

63.{

64. max_heap * new_heap = NULL ;

65.

66. / ** <检查参数* /

67. if （cap <= 0 ）

68. {

69. fprintf （stderr ，“alloc_max_heap：invalid parameter。\

n” ）;

70. 返回 NULL ;

71. }

72.

73. / ** <分配内存* /

74. new_heap = （max_heap *）malloc的（的 sizeof （max_heap ））;

75. if （NULL == new_heap ）

76. {

77. fprintf （stderr ，“alloc_max_heap：malloc失败。\ n” ）;

78. 返回 NULL ;

79. }

80.

81. new_heap - > elem = （int *）calloc （cap ，sizeof （int ））;

82. if （NULL == new_heap - > elem ）

83. {

84. fprintf （stderr ，“alloc_max_heap：calloc失败。\ n” ）;

85. free （new_heap ），new_heap = NULL ; / ** <免费分配内存。* /

86. 返回 NULL ;

87. }

88.

89. / ** <设置其他参数。* /

90. new_heap - > capacity = cap ;

91. new_heap - > current_index = 0 ;

92.

93. 返回 new_heap ;

94.}

95.

96./ ** <destroy max_heap * /

97.空隙 destroy_max_heap （max_heap ** 堆）

98.{

99. if （NULL ！= 堆）

100. {

101. if （（* heap ） - > elem ）

102. {

103. free （（* heap ） - > elem ）;

104. （* heap ） - > elem = NULL ;

105. }

106.

107. free （* heap ）;

108. * heap = NULL ;

109. }

110. }

111.

112. / ** <交换元素* /

113. void swap_int （int * number ，int lhs ，int rhs ）

114. {

115. int tmp = number [ lhs ];

116. number [ lhs ] = number [ rhs ];

117. number [ rhs ] = tmp ;

118. }

119.

120. / ** <插值* /

121. 空隙 push_value （max_heap * 堆，常量 INT 值）

122. {

123. int left_child = 0 ，

124. right_child = 0 ，

125. parent_index = 0 ;

126.

127. / ** <检查参数* /

128. if （NULL == heap ）

129. {

130. fprintf （stderr ，“push_value：invalid parameter。\ n” ）;

131. 回归;

132. }

133. if （heap - > current_index == heap - > capacity ）

134. {

135. fprintf （stderr ，“push_value：这个 max_heap已满。\ n” ）;

136. 回归;

137. }

138.

139. / ** <推送元素* /

140. parent_index = heap - > current_index ;

141. heap - > elem [ heap - > current_index ++] = value ;

142.

143. while （parent_index > = 0 ）

144. {

145. left_child = 2 * parent_index + 1 ;

146. right_child = 2 * parent_index + 2 ;

147.

148. if （left_child < heap - > current_index

149. && heap - > elem [ left_child ] > heap - > elem [

parent_index ] ）

150. {

151. swap_int （heap - > elem ，left_child ，parent_index ）;

152. }

153.

154. if （right_child < heap - > current_index

155. && heap - > elem [ right_child ] > heap - > elem [

parent_index ] ）

156. {

157. swap_int（heap->elem，right_child，parent_index）;

158. }

159.

160. - parent_index ;

161. }

162. }

163.

164. / ** <pop元素* /

165. INT pop_value （max_heap * 堆）

166. {

167. int ret = 0 ，

168. left_child = 0 ，

169. right_child = 0 ，

170. parent_index = 0 ，

171. swap_index = 0 ;

172.

173. / ** <检查参数。* /

174. if （NULL == heap ）

175. {

176. fprintf （stderr ，“pop_value：invalid parameter。\ n” ）;

177. 返回- 1 ;

178. }

179. if （heap - > current_index == 0 ）

180. {

181. fprintf （stderr ，“pop_value：此 max_heap为空。\ n” ）;

182. 返回- 1 ;

183. }

184.

185. / ** <pop元素* /

186. - heap - > current_index ;

187. swap_int （heap - > elem ，0 ，heap - > current_index ）;

188. ret = heap - > elem [ heap - > current_index ];

189. parent_index = 0 ;

190. while （parent_index < heap - > current_index ）

191. {

192. left_child = 2 * parent_index + 1 ;

193. right_child = 2 * parent_index + 2 ;

194.

195. / ** <寻找左右之间的最大值。* /

196. if （left_child < heap - > current_index

197. && right_child < heap - > current_index ）

198. {

199. swap_index = heap - > elem [ left_child ] > heap - > elem

[ right_child ] ？

200. left_child ：right_child ;

201. }

202. else if （left_child < heap - > current_index ）

203. {

204. swap_index = left_child ;

205. }

206. else if （right_child < heap - > current_index ）

207. {

208. swap_index = right_child ;

209. }

210.

211. / ** <if heap-> elem [swap_index]大于 heap-> elem [parent]，

互相交换。* /

212. if（heap->elem[swap_index]>heap->elem[parent_index

] ）

213. {

214. swap_int （heap - > elem ，swap_index ，parent_index ）;

215. parent_index = swap_index ;

216. }

217. 其他

218. {

219. 打破;

220. }

221. }

222.

223. 返回 RET ;

224. }

225.

226. / ** <test max_heap * /

227. void test_max_heap （）

228. {

229. INT 数[] = { 79 ，81 ，97 ，21 ，88 ，15 ，1 ，61 ，28 ，68 };

230. int size_i = sizeof （number ）/ sizeof （int ）;

231. int index = 0 ;

232. max_heap * heap = NULL ;

233.

234. heap = alloc_max_heap （size_i ）;

235. for （index = 0 ; index < size_i ; ++ index ）

236. {

237. push_value （heap ，number [ index ] ）;

238. }

239.

240. printf （“\ n \ n \ n \ n \ n \ n上的元素是\ n” ）;

241. while （heap - > current_index ）

242. {

243. index = pop_value （heap ）;

244. printf （“％d” ，index ）;

245. }

246. printf （“\ n” ）;

247.

248. destroy_max_heap （＆heap ）;

249. }

250.

251. / ** <生成数据* /

252. void generating_data （const char * file_name ，const int size_i ，

const int range ）

253. {

254. FILE * output_file = NULL ;

255. int index = 0 ，

256. data = 0 ;

257.

258. / ** <检查参数* /

259. if （NULL == file_name || size_i <= 0 || range <= 0 ）

260. {

261. fprintf （stderr ，“generating_data：invalid parameter。\

n” ）;

262. 回归;

263. }

264.

265. / ** <打开文件。* /

266. output_file = fopen （file_name ，“w” ）;

267. if （NULL == output_file ）

268. {

269. fprintf （stderr ，“generating_data：无法打开文件％s。\

n” ，file_name ）;

270. 回归;

271. }

272.

273. / ** <将数据写入文件。* /

274. srand （（无符号）时间（0 ））;

275. while （index < size_i ）

276. {

277. data = rand （）％range ;

278. fprintf （output_file ，“％d \ n” ，data ）;

279. ++ index ;

280. }

281.

282. fclose （output_file ）;

283. }

284.

285. / ** <输出 min k元素* /

286. void output_min_k_elements （const char * file_name ，const int k

）

287. {

288. max_heap * heap = NULL ;

289. FILE * input_file = NULL ;

290. int data = 0 ，

291. index = k ;

292.

293. / ** <检查参数。* /

294. if （NULL == file_name || k <= 0 ）

295. {

296. fprintf （stderr ，“output_min_k_elements：invalid

parameter。\ n” ）;

297. 回归;

298. }

299.

300. / ** <打开文件* /

301. input_file = fopen （file_name ，“r” ）;

302. if （NULL == input_file ）

303. {

304. fprintf （stderr ，“output_min_k_elements：无法打开文件％

s。\ n” ，file_name ）;

305. 回归;

306. }

307.

308. heap = alloc_max_heap （k ）;

309. / ** <填充堆* /

310. while （index - > 0 && fscanf （input_file ，“％d” ，＆data ）！=

EOF ）

311. {

312. push_value （堆，数据）;

313. }

314.

315. while （fscanf （input_file ，“％d” ，＆data ）！= EOF ）

316. {

317. if （heap - > current_index == k

318. && data < heap - > elem [ 0 ] ）

319. {

320. pop_value （堆）;

321. push_value （堆，数据）;

322. }

323. }

324.

325. / ** <输出数据* /

326. while （heap - > current_index ）

327. {

328. data = pop_value （heap ）;

329. printf （“％d” ，数据）;

330. }

331.

332. / ** <可用内存和关闭文件。* /

333. destroy_max_heap （＆heap ）;

334. fclose （input_file ）;

335. }

336.

337. / ** <test * /

338. void test_func （void ）

339. {

340. char * file_name = “e：\\ test.txt” ;

341. int k = 10 ;

342. int size_i = 100000 ;

343. int range = 100000000 ;

344.

345. / ** <生成数据* /

346. generating_data （file_name ，size_i ，range ）;

347. output_min_k_elements （file_name ，k ）;

348. }

349.

350. int main （）

351. {

352. test_func （）;

353.

354. 返回 0 ;

355. }

356.

1.

357. 4. 通过随机函数生成随机数填充一个二维数组，随机数被 4 取余，其中 0 表示

红色，1表示蓝色，2表示绿色，3表示黑色。根据五子棋的规则，5个连续在一起

的输出。

1.

2.

1. #include <stdio.h>

3. #include <stdlib.h>

4. #include <time.h>

5.

6. void gobang_problem （int colors [] [ 10 ]，int size_i ）

7. {

8. int i ，j ，k ，p ，

9. colors_cnt = 0 ;

10.

11. / ** <检查参数。* /

12. if （NULL == colors || size_i <= 0 ）

13. {

14. fprintf （stderr ，“gobang_problem：invalid argument。\

n” ）;

15. 回归;

16. }

17.

18. / ** <compute * /

19. for （i = 0 ; i < size_i ; ++ i ）

20. {

21. for （j = 0 ; j < size_i ; ++ j ）

22. {

23. / ** <纵向* /

24. for （colors_cnt = 0 ，k = j ; k < size_i ; ++ k ）

25. {

26. if （colors [ i ] [ j ] == colors [ i ] [ k ] ）

27. {

28. colors_cnt ++;

29. / ** <如果找到，输出。* /

30. if （colors_cnt == 5 ）

31. {

32. 而（k > = j ）

33. {

34. printf （“％d％d” ，i ，k - ）;

35. }

36.

37. 回归;

38. }

39. }

40. 其他

41. {

42. 打破; / ** <如果不相同，则不需要继续。* /

43. }

44. }

45.

46. / ** <横向* /

47. for （colors_cnt = 0 ，k = i ; k < size_i ; ++ k ）

48. {

49. if （colors [ i ] [ j ] == colors [ k ] [ j ] ）

50. {

51. colors_cnt ++;

52. if （colors_cnt == 5 ）

53. {

54. 而（k > = i ）

55. {

56. printf （“％d％d，” ，k - ，j ）;

57. }

58.

59. 回归;

60. }

61.

62. }

63. 其他

64. {

65. 打破;

66. }

67. }

68.

69. / ** <向下斜向* /

70. for （colors_cnt = 0 ，k = i ，p = j ;

71. k < size_i && p < size_i ; ++ k ，++ p ）

72. {

73. if （colors [ i ] [ j ] == colors [ k ] [ p ] ）

74. {

75. colors_cnt ++;

76. if （colors_cnt == 5 ）

77. {

78. 而（k > = i && p > = j ）

79. {

80. printf （“％d％d” ，k - ，p - ）;

81. }

82.

83. 回归;

84. }

85.

86. } //结束如果

87. 其他

88. {

89. 打破;

90. }

91. } //结束

92. } //结束

93. } //结束

94.

95.

96. / ** <向上斜向* /

97. for （i = size_i - 1 ; i > = 0 ; - i ）

98. {

99. for （j = 0 ; j < size_i ; ++ j ）

100. {

101. for （colors_cnt = 0 ，k = i ，p = j ;

102. k > = 0 && p < size_i ;

103. - k ，++ p ）

104. {

105. if （colors [ i ] [ j ] == colors [ k ] [ p ] ）

106. {

107. colors_cnt ++;

108. if （colors_cnt == 5 ）

109. {

110. 而（k <= i && p > = j ）

111. {

112. printf （“％d％d，” ，k ++，p - ）;

113. }

114.

115. 回归;

116. }

117.

118. }

119. 其他

120. {

121. 打破;

122. }

123. } //结束

124. }

125. }

126. }

127.

128. void generate_chessboard （int chessboard [] [ 10 ]，int size_i ）

129. {

130. int i ，j ;

131.

132. / ** <检查参数* /

133. if （NULL == chessboard || size_i <= 0 ）

134. {

135. fprintf（stderr，“generate_chessboard：invalid argument。

\ n” ）;

136. 回归;

137. }

138.

139. / ** <生成* /

140. srand （（无符号）时间（0 ））;

141. for （i = 0 ; i < size_i ; ++ i ）

142. {

143. for （j = 0 ; j < size_i ; ++ j ）

144. {

145. 棋盘[ i ] [ j ] = 兰德（）％4 ;

146. }

147. }

148.

149. / ** <输出棋盘* /

150. printf （“\ n棋盘中的这种颜色是\ n” ）;

151. for （i = 0 ; i < size_i ; ++ i ）

152. {

153. for （j = 0 ; j < size_i ; ++ j ）

154. {

155. printf （“％d” ，chessboard [ i ] [ j ] ）;

156. }

157. printf （“\ n” ）;

158. }

159. }

160.

161. int main （）

162. {

163. int chessboard [ 10 ] [ 10 ];

164. int size_i = 10 ;

165.

166. generate_chessboard （chessboard ，size_i ）;

167. printf （“\ n五种颜色低：\ n” ）;

168. gobang_problem （chessboard ，size_i ）;

169.

170. 返回 0 ;

171. }

172.

173.

