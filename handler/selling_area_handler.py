[09:47:48] 📦 Processed dependencies!




────────────────────── Traceback (most recent call last) ───────────────────────

  /home/adminuser/venv/lib/python3.13/site-packages/streamlit/runtime/scriptru  

  nner/exec_code.py:128 in exec_func_with_error_handling                        

                                                                                

  /home/adminuser/venv/lib/python3.13/site-packages/streamlit/runtime/scriptru  

  nner/script_runner.py:667 in code_to_exec                                     

                                                                                

  /home/adminuser/venv/lib/python3.13/site-packages/streamlit/runtime/scriptru  

  nner/script_runner.py:165 in _mpa_v1                                          

                                                                                

  /home/adminuser/venv/lib/python3.13/site-packages/streamlit/navigation/page.  

  py:300 in run                                                                 

                                                                                

  /mount/src/testingamas/pages/selling_area.py:57 in <module>                   

                                                                                

    54 if RUN:                                                                  

    55 │   now = time.time()                                                    

    56 │   if now - st.session_state["s_last"] >= INTERVAL:                     

  ❱ 57 │   │   st.session_state["s_log"] = cycle()                              

    58 │   │   st.session_state["s_last"] = now                                 

    59 │   │   st.session_state["s_cycles"] += 1                                

    60                                                                          

                                                                                

  /mount/src/testingamas/pages/selling_area.py:47 in cycle                      

                                                                                

    44 │   df["threshold"] = df["shelfthreshold"].fillna(0)                     

    45 │   df["average"]   = df["shelfaverage"].fillna(df["threshold"])         

    46 │   below = df[df.totalqty < df.threshold].copy()                        

  ❱ 47 │   if below.empty():                                                    

    48 │   │   return []                       # ← guard keeps log empty if no  

    49 │   below["need"] = below["average"] - below["totalqty"]                 

    50 │   below = below[below.need > 0]                                        

────────────────────────────────────────────────────────────────────────────────

TypeError: 'bool' object is not callable
